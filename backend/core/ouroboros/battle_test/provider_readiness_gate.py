"""Battle-test Provider-Readiness Gate
=======================================

**The pre-boot probe that fails the soak BEFORE provider spend
when the upstream provider stack is unhealthy.**

Closes the failure mode the SWE v18 ``bt-2026-05-16-175621``
diagnosis exposed: Anthropic Claude API was returning 500
Internal_server_error for ≥5 consecutive requests during the
session window. The orchestrator handled the failures correctly
(failed loudly, retried within budget, exhausted cleanly, cancelled
ops at deadline) — but the soak still ran 2,337 seconds, burned
$0.26 of Claude budget, and produced zero candidates before
hitting idle timeout.

The structural fix is to refuse soak start when the providers
are not in a state to emit candidates. Two complementary signals:

  1. **Live circuit-breaker state** — if the canonical
     :class:`claude_circuit_breaker.ClaudeCircuitBreaker` is
     already OPEN at boot, recent failures already exceeded the
     trip threshold; starting another soak will just exhaust again.
  2. **Active health probe** — even if the CB is CLOSED (fresh
     process; no recent traffic), a synchronous ping confirms the
     provider is responsive RIGHT NOW. Catches the "first request
     of the session is a 500" pattern that v18 exhibited.

Composition contract (operator-binding 2026-05-16, "SUPER DUPER
BEEF IT UP"):

  * **NO parallel state** — composes canonical
    :func:`get_claude_circuit_breaker` (read-only snapshot) +
    :meth:`ClaudeProvider.health_probe` +
    :meth:`DoublewordProvider.health_probe` exclusively.
  * **NO hardcoded triggers** — every threshold is an env knob.
  * **NEVER raises** — every entry point yields a frozen
    :class:`ProviderReadinessReport` even on import failures,
    probe timeouts, or canceled probes.
  * **NO trust bypass** — the gate refuses the soak when
    unhealthy; it does NOT silently lower thresholds or retry
    storm. Operator must explicitly disable the gate or wait
    for the provider stack to recover.

Authority asymmetry (AST-pinned): stdlib + canonical Claude /
DW providers + canonical claude_circuit_breaker ONLY. NEVER
imports orchestrator / iron_gate / policy / candidate_generator /
urgency_router / change_engine / semantic_guardian /
auto_committer / risk_tier_floor / tool_executor /
plan_generator. The gate is a pre-flight probe, not a decision
authority.

Operator-decision contract: when this gate fires, the soak does
NOT start. No state is mutated; no envelopes are dispatched; no
provider call beyond the probe is made. The structured report
is written to the session directory so post-hoc diagnosis is
trivial — the operator sees WHY the soak refused at the first
log line, not after 27 minutes of EXHAUSTION events.
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


PROVIDER_READINESS_GATE_SCHEMA_VERSION: str = (
    "provider_readiness_gate.1"
)


# ---------------------------------------------------------------------------
# Env knobs — operator-tunable, no hardcoding
# ---------------------------------------------------------------------------


_ENV_MASTER = (
    "JARVIS_BATTLE_TEST_PROVIDER_READINESS_GATE_ENABLED"
)
_ENV_CLAUDE_PROBE_TIMEOUT_S = (
    "JARVIS_BATTLE_TEST_PROVIDER_READINESS_CLAUDE_PROBE_TIMEOUT_S"
)
_ENV_DW_PROBE_TIMEOUT_S = (
    "JARVIS_BATTLE_TEST_PROVIDER_READINESS_DW_PROBE_TIMEOUT_S"
)
_ENV_REQUIRE_DW = (
    "JARVIS_BATTLE_TEST_PROVIDER_READINESS_REQUIRE_DW"
)
_ENV_REPORT_PATH = (
    "JARVIS_BATTLE_TEST_PROVIDER_READINESS_REPORT_PATH"
)


# Defaults — chosen for the "fail-fast before spend" contract.
# Claude probe: 10s default is generous for a single one-token
# request but bounds the boot delay. DW probe: 5s default
# because the /models endpoint is much cheaper.
# require_dw default-FALSE because DW is route-conditional — for
# IMMEDIATE-routed ops the candidate_generator zero-budgets DW,
# so requiring DW to be up for those soaks would be overzealous.
_DEFAULT_CLAUDE_PROBE_TIMEOUT_S: float = 10.0
_DEFAULT_DW_PROBE_TIMEOUT_S: float = 5.0


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def _env_float(
    name: str, *, default: float, lo: float, hi: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def master_enabled() -> bool:
    """§33.1 master gate. Default-FALSE. NEVER raises.

    Operator flips to True in production soak env blocks once the
    gate has been graduated. Default-FALSE preserves the
    pre-existing battle-test behavior byte-identically until the
    operator opts in."""
    return _env_truthy(_ENV_MASTER, default=False)


def claude_probe_timeout_s() -> float:
    """``JARVIS_BATTLE_TEST_PROVIDER_READINESS_CLAUDE_PROBE_TIMEOUT_S``
    — wall-clock cap on the Claude probe. Clamped [1, 60].
    Default 10s. The probe is a single ``messages.create`` with
    max_tokens=1; the timeout caps boot delay when Claude is
    unresponsive."""
    return _env_float(
        _ENV_CLAUDE_PROBE_TIMEOUT_S,
        default=_DEFAULT_CLAUDE_PROBE_TIMEOUT_S,
        lo=1.0, hi=60.0,
    )


def dw_probe_timeout_s() -> float:
    """``JARVIS_BATTLE_TEST_PROVIDER_READINESS_DW_PROBE_TIMEOUT_S``
    — wall-clock cap on the DW ``/models`` ping. Clamped
    [1, 30]. Default 5s."""
    return _env_float(
        _ENV_DW_PROBE_TIMEOUT_S,
        default=_DEFAULT_DW_PROBE_TIMEOUT_S,
        lo=1.0, hi=30.0,
    )


def require_dw() -> bool:
    """``JARVIS_BATTLE_TEST_PROVIDER_READINESS_REQUIRE_DW`` —
    when True, gate refuses soak start if DW probe fails (even
    when Claude is healthy). When False (default), DW probe
    runs informationally — its result is captured in the report
    but doesn't block the soak. Default-FALSE because
    IMMEDIATE-routed ops zero-budget DW (the canonical routing
    policy); requiring DW for IMMEDIATE-only soaks would be
    overzealous."""
    return _env_truthy(_ENV_REQUIRE_DW, default=False)


# ---------------------------------------------------------------------------
# Closed taxonomy — bytes-pinned via AST
# ---------------------------------------------------------------------------


class ReadinessVerdict(str, enum.Enum):
    """Closed 7-value taxonomy.

    Each verdict carries a distinct operator action:
      * READY → soak proceeds normally
      * DISABLED → gate master-flag off; preserves pre-existing
        battle-test behavior byte-identically
      * CB_OPEN → upstream circuit breaker already tripped;
        operator should investigate recent failures before re-soak
      * CLAUDE_PROBE_FAILED → Claude probe returned False or
        timed out; the IMMEDIATE / Claude-direct routes have no
        viable path
      * DW_PROBE_FAILED → DW probe failed and require_dw=True
      * BOTH_UNHEALTHY → Claude AND DW both failed (only fires
        when require_dw=True)
      * ERROR → gate itself failed (import error, etc.) —
        defensive bottom verdict; operator should investigate
        the gate, not the providers
    """

    READY = "ready"
    DISABLED = "disabled"
    CB_OPEN = "cb_open"
    CLAUDE_PROBE_FAILED = "claude_probe_failed"
    DW_PROBE_FAILED = "dw_probe_failed"
    BOTH_UNHEALTHY = "both_unhealthy"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Frozen result containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    """One provider's probe outcome. Frozen."""

    provider: str
    """``"claude"`` | ``"doubleword"``. The provider that was probed."""

    healthy: bool
    """True iff the probe succeeded within the timeout."""

    elapsed_s: float = 0.0
    err_class: str = ""
    """``"TimeoutError"`` / ``"APIStatusError"`` / etc. Empty
    when healthy=True."""
    err_msg: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider[:32],
            "healthy": bool(self.healthy),
            "elapsed_s": float(self.elapsed_s),
            "err_class": self.err_class[:64],
            "err_msg": self.err_msg[:256],
        }


@dataclass(frozen=True)
class CircuitBreakerSnapshot:
    """Snapshot of the canonical CB state at gate time. Frozen.
    Read-only — the gate does NOT mutate the CB."""

    available: bool
    """True iff get_claude_circuit_breaker module + singleton
    were resolvable. False on import failure (gate degrades to
    probe-only mode + records the failure)."""

    enabled: bool = False
    """The CB's own master flag state (canonical
    ``claude_circuit_breaker.is_enabled``)."""

    should_allow: bool = True
    """Direct ``should_allow_request()`` call result. False ⇒
    CB is OPEN AND recovery-window not yet elapsed."""

    state: str = ""
    """CircuitState.name: CLOSED / OPEN / HALF_OPEN."""

    consecutive_failures: int = 0
    total_trips: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": bool(self.available),
            "enabled": bool(self.enabled),
            "should_allow": bool(self.should_allow),
            "state": self.state[:32],
            "consecutive_failures": int(self.consecutive_failures),
            "total_trips": int(self.total_trips),
        }


@dataclass(frozen=True)
class ProviderReadinessReport:
    """Aggregate result of one readiness check.

    Frozen + JSON-projectable so the harness can write
    ``provider_readiness.json`` to the session directory and
    operator can grep verdict / diagnostic without parsing
    free-form logs."""

    verdict: ReadinessVerdict
    cb_snapshot: CircuitBreakerSnapshot
    probes: tuple = field(default_factory=tuple)
    """Tuple of :class:`ProbeResult`; one per probed provider."""

    elapsed_s: float = 0.0
    diagnostic: str = ""
    schema_version: str = field(
        default=PROVIDER_READINESS_GATE_SCHEMA_VERSION,
    )

    @property
    def soak_should_proceed(self) -> bool:
        """True iff the verdict permits soak start."""
        return self.verdict in (
            ReadinessVerdict.READY,
            ReadinessVerdict.DISABLED,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "soak_should_proceed": self.soak_should_proceed,
            "cb_snapshot": self.cb_snapshot.to_dict(),
            "probes": [p.to_dict() for p in self.probes],
            "elapsed_s": float(self.elapsed_s),
            "diagnostic": self.diagnostic[:512],
        }


# ---------------------------------------------------------------------------
# Internal — compose canonical surfaces
# ---------------------------------------------------------------------------


def _read_circuit_breaker_snapshot() -> CircuitBreakerSnapshot:
    """Compose canonical
    :func:`claude_circuit_breaker.get_claude_circuit_breaker`.
    Read-only; NEVER mutates the CB. NEVER raises — import or
    runtime failures degrade to ``available=False``.
    """
    try:
        from backend.core.ouroboros.governance.claude_circuit_breaker import (  # noqa: E501
            get_claude_circuit_breaker,
            is_enabled,
        )
    except Exception:  # noqa: BLE001 — defensive
        return CircuitBreakerSnapshot(available=False)
    try:
        breaker = get_claude_circuit_breaker()
        enabled = bool(is_enabled())
        # The canonical .snapshot() returns a dict with full state.
        snap = breaker.snapshot()
        return CircuitBreakerSnapshot(
            available=True,
            enabled=enabled,
            should_allow=bool(
                breaker.should_allow_request(),
            ),
            state=str(snap.get("state", ""))[:32],
            consecutive_failures=int(
                snap.get("consecutive_transport_failures", 0) or 0,
            ),
            total_trips=int(
                snap.get("total_trips", 0) or 0,
            ),
        )
    except Exception as err:  # noqa: BLE001 — defensive
        logger.debug(
            "[provider_readiness_gate] CB snapshot raised: %r",
            err,
        )
        return CircuitBreakerSnapshot(available=False)


async def _probe_claude(
    provider: Optional[Any] = None,
    *,
    timeout: Optional[float] = None,
) -> ProbeResult:
    """Run :meth:`ClaudeProvider.health_probe` with wall-clock
    timeout. NEVER raises."""
    started = time.monotonic()
    cap = (
        float(timeout) if timeout is not None
        else claude_probe_timeout_s()
    )
    if provider is None:
        # Compose canonical Claude provider. ClaudeProvider requires
        # an explicit api_key — pull from ANTHROPIC_API_KEY (canonical
        # env name; same one ClaudeProvider.generate() ultimately uses
        # via the Anthropic SDK).
        try:
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                return ProbeResult(
                    provider="claude",
                    healthy=False,
                    elapsed_s=max(
                        0.0, time.monotonic() - started,
                    ),
                    err_class="MissingAPIKey",
                    err_msg=(
                        "ANTHROPIC_API_KEY not set; "
                        "cannot probe Claude"
                    ),
                )
            from backend.core.ouroboros.governance.providers import (
                ClaudeProvider,
            )
            provider = ClaudeProvider(api_key=api_key)
        except Exception as err:  # noqa: BLE001
            return ProbeResult(
                provider="claude",
                healthy=False,
                elapsed_s=max(0.0, time.monotonic() - started),
                err_class=type(err).__name__,
                err_msg=f"ClaudeProvider init: {err}"[:256],
            )
    try:
        ok = await asyncio.wait_for(
            provider.health_probe(), timeout=cap,
        )
        return ProbeResult(
            provider="claude",
            healthy=bool(ok),
            elapsed_s=max(0.0, time.monotonic() - started),
            err_class=(
                "" if ok else "HealthProbeReturnedFalse"
            ),
            err_msg=(
                "" if ok else
                "health_probe() returned False"
            ),
        )
    except asyncio.TimeoutError:
        return ProbeResult(
            provider="claude",
            healthy=False,
            elapsed_s=max(0.0, time.monotonic() - started),
            err_class="TimeoutError",
            err_msg=f"probe timed out at {cap:.1f}s",
        )
    except asyncio.CancelledError:
        return ProbeResult(
            provider="claude",
            healthy=False,
            elapsed_s=max(0.0, time.monotonic() - started),
            err_class="CancelledError",
            err_msg="probe cancelled",
        )
    except Exception as err:  # noqa: BLE001
        return ProbeResult(
            provider="claude",
            healthy=False,
            elapsed_s=max(0.0, time.monotonic() - started),
            err_class=type(err).__name__,
            err_msg=str(err)[:256],
        )


async def _probe_doubleword(
    provider: Optional[Any] = None,
    *,
    timeout: Optional[float] = None,
) -> ProbeResult:
    """Run :meth:`DoublewordProvider.health_probe` with timeout.
    NEVER raises."""
    started = time.monotonic()
    cap = (
        float(timeout) if timeout is not None
        else dw_probe_timeout_s()
    )
    if provider is None:
        try:
            from backend.core.ouroboros.governance.doubleword_provider import (  # noqa: E501
                DoublewordProvider,
            )
            provider = DoublewordProvider()
        except Exception as err:  # noqa: BLE001
            return ProbeResult(
                provider="doubleword",
                healthy=False,
                elapsed_s=max(0.0, time.monotonic() - started),
                err_class=type(err).__name__,
                err_msg=f"DoublewordProvider init: {err}"[:256],
            )
    try:
        ok = await asyncio.wait_for(
            provider.health_probe(), timeout=cap,
        )
        return ProbeResult(
            provider="doubleword",
            healthy=bool(ok),
            elapsed_s=max(0.0, time.monotonic() - started),
            err_class=(
                "" if ok else "HealthProbeReturnedFalse"
            ),
        )
    except asyncio.TimeoutError:
        return ProbeResult(
            provider="doubleword",
            healthy=False,
            elapsed_s=max(0.0, time.monotonic() - started),
            err_class="TimeoutError",
            err_msg=f"probe timed out at {cap:.1f}s",
        )
    except asyncio.CancelledError:
        return ProbeResult(
            provider="doubleword",
            healthy=False,
            elapsed_s=max(0.0, time.monotonic() - started),
            err_class="CancelledError",
        )
    except Exception as err:  # noqa: BLE001
        return ProbeResult(
            provider="doubleword",
            healthy=False,
            elapsed_s=max(0.0, time.monotonic() - started),
            err_class=type(err).__name__,
            err_msg=str(err)[:256],
        )


def _classify_verdict(
    cb_snap: CircuitBreakerSnapshot,
    claude_probe: ProbeResult,
    dw_probe: Optional[ProbeResult],
    *,
    require_dw_flag: bool,
) -> tuple:
    """Compute the aggregate verdict + diagnostic. Pure function.
    NEVER raises."""
    # CB state is the strongest signal — if the breaker is OPEN,
    # recent failures already crossed the trip threshold. We
    # only refuse on the EFFECTIVE allow signal (should_allow=False)
    # which respects the recovery window. A fresh breaker that's
    # never seen traffic returns should_allow=True even if
    # enabled=False (the CB is closed by construction).
    if cb_snap.available and not cb_snap.should_allow:
        return (
            ReadinessVerdict.CB_OPEN,
            (
                f"Claude circuit breaker not allowing requests "
                f"(state={cb_snap.state!r}, consecutive_failures="
                f"{cb_snap.consecutive_failures}, total_trips="
                f"{cb_snap.total_trips}). The CB is the canonical "
                f"signal that the provider stack should not be "
                f"called right now."
            ),
        )
    # Both unhealthy when require_dw is set + both probes failed.
    if (
        require_dw_flag
        and not claude_probe.healthy
        and dw_probe is not None
        and not dw_probe.healthy
    ):
        return (
            ReadinessVerdict.BOTH_UNHEALTHY,
            (
                f"Both providers unhealthy. Claude: "
                f"{claude_probe.err_class}: "
                f"{claude_probe.err_msg!r}. "
                f"DW: {dw_probe.err_class}: "
                f"{dw_probe.err_msg!r}."
            ),
        )
    # DW-only failure path (only fires when DW is required).
    if (
        require_dw_flag
        and dw_probe is not None
        and not dw_probe.healthy
    ):
        return (
            ReadinessVerdict.DW_PROBE_FAILED,
            (
                f"DW probe failed (require_dw=true): "
                f"{dw_probe.err_class}: {dw_probe.err_msg!r}"
            ),
        )
    if not claude_probe.healthy:
        return (
            ReadinessVerdict.CLAUDE_PROBE_FAILED,
            (
                f"Claude probe failed: {claude_probe.err_class}: "
                f"{claude_probe.err_msg!r}. IMMEDIATE / Claude-"
                f"direct routes have no viable path."
            ),
        )
    return (
        ReadinessVerdict.READY,
        (
            f"Claude probe OK ({claude_probe.elapsed_s:.2f}s)"
            + (
                f"; DW probe OK ({dw_probe.elapsed_s:.2f}s)"
                if (dw_probe is not None and dw_probe.healthy)
                else (
                    f"; DW probe failed but require_dw=false "
                    f"(informational only)"
                    if (
                        dw_probe is not None
                        and not dw_probe.healthy
                    ) else ""
                )
            )
        ),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def check_provider_readiness(
    *,
    claude_provider: Optional[Any] = None,
    doubleword_provider: Optional[Any] = None,
    probe_dw: Optional[bool] = None,
    claude_timeout_override: Optional[float] = None,
    dw_timeout_override: Optional[float] = None,
    require_dw_override: Optional[bool] = None,
    cb_snapshot_override: Optional[CircuitBreakerSnapshot] = None,
) -> ProviderReadinessReport:
    """Probe canonical providers + read CB state. Returns a frozen
    aggregate report. NEVER raises.

    Parameters
    ----------
    claude_provider / doubleword_provider:
        Operator-injectable providers (used by tests). When None,
        the gate composes canonical singletons via lazy import.
    probe_dw:
        When True, run the DW probe even if require_dw is False
        (so the report carries DW health informationally).
        When False, skip the DW probe entirely. When None
        (default), probe iff require_dw is True.
    *_override:
        Per-call overrides for env-resolved tunables. Tests use
        these; production callers leave them None.
    cb_snapshot_override:
        Test seam — bypasses canonical CB read. Production
        callers leave this None.
    """
    started = time.monotonic()

    # Master-flag gate — when disabled, return verdict=DISABLED.
    # The harness short-circuits to "soak should proceed" so the
    # gate's master-flag-off path is byte-identical to no gate
    # at all.
    if not master_enabled():
        return ProviderReadinessReport(
            verdict=ReadinessVerdict.DISABLED,
            cb_snapshot=CircuitBreakerSnapshot(available=False),
            elapsed_s=max(0.0, time.monotonic() - started),
            diagnostic=(
                f"{_ENV_MASTER}=false (§33.1 default; explicit "
                f"opt-in required)"
            ),
        )

    # CB read — defensive wrapper inside _read_circuit_breaker_
    # snapshot, but we add another try/except at the boundary so
    # the gate's NEVER-raises contract holds even if the helper
    # somehow leaks an exception.
    try:
        cb_snap = (
            cb_snapshot_override
            if cb_snapshot_override is not None
            else _read_circuit_breaker_snapshot()
        )
    except Exception as err:  # noqa: BLE001 — defensive
        return ProviderReadinessReport(
            verdict=ReadinessVerdict.ERROR,
            cb_snapshot=CircuitBreakerSnapshot(available=False),
            elapsed_s=max(0.0, time.monotonic() - started),
            diagnostic=(
                f"CB snapshot raised: {type(err).__name__}"
            ),
        )

    # Claude probe — always.
    try:
        claude_probe = await _probe_claude(
            claude_provider,
            timeout=claude_timeout_override,
        )
    except Exception as err:  # noqa: BLE001 — defensive
        return ProviderReadinessReport(
            verdict=ReadinessVerdict.ERROR,
            cb_snapshot=cb_snap,
            elapsed_s=max(0.0, time.monotonic() - started),
            diagnostic=(
                f"Claude probe raised: {type(err).__name__}"
            ),
        )

    # DW probe — conditional.
    eff_require_dw = (
        bool(require_dw_override)
        if require_dw_override is not None
        else require_dw()
    )
    should_probe_dw = (
        eff_require_dw if probe_dw is None else bool(probe_dw)
    )
    dw_probe: Optional[ProbeResult] = None
    if should_probe_dw:
        try:
            dw_probe = await _probe_doubleword(
                doubleword_provider,
                timeout=dw_timeout_override,
            )
        except Exception as err:  # noqa: BLE001 — defensive
            return ProviderReadinessReport(
                verdict=ReadinessVerdict.ERROR,
                cb_snapshot=cb_snap,
                probes=(claude_probe,),
                elapsed_s=max(0.0, time.monotonic() - started),
                diagnostic=(
                    f"DW probe raised: {type(err).__name__}"
                ),
            )

    # Aggregate verdict.
    probes_tuple: tuple = (
        (claude_probe, dw_probe)
        if dw_probe is not None else (claude_probe,)
    )
    verdict, diag = _classify_verdict(
        cb_snap, claude_probe, dw_probe,
        require_dw_flag=eff_require_dw,
    )
    return ProviderReadinessReport(
        verdict=verdict,
        cb_snapshot=cb_snap,
        probes=probes_tuple,
        elapsed_s=max(0.0, time.monotonic() - started),
        diagnostic=diag,
    )


# ---------------------------------------------------------------------------
# Report persistence — the operator-greppable artifact
# ---------------------------------------------------------------------------


def write_readiness_report(
    report: ProviderReadinessReport,
    *,
    session_dir: Optional[Path] = None,
    path_override: Optional[Path] = None,
) -> Optional[Path]:
    """Write ``provider_readiness.json`` to the session directory.
    NEVER raises — returns the written path or None on failure.

    Path resolution order:
      1. ``path_override`` (test / explicit caller)
      2. ``JARVIS_BATTLE_TEST_PROVIDER_READINESS_REPORT_PATH`` env
      3. ``<session_dir>/provider_readiness.json``
      4. ``.ouroboros/sessions/last/provider_readiness.json``
    """
    try:
        target: Optional[Path] = None
        if path_override is not None:
            target = Path(path_override)
        else:
            env_path = os.environ.get(
                _ENV_REPORT_PATH, "",
            ).strip()
            if env_path:
                target = Path(env_path)
            elif session_dir is not None:
                target = (
                    Path(session_dir) / "provider_readiness.json"
                )
            else:
                target = (
                    Path(".ouroboros") / "sessions" / "last"
                    / "provider_readiness.json"
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                report.to_dict(),
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return target
    except Exception as err:  # noqa: BLE001 — defensive
        logger.debug(
            "[provider_readiness_gate] write_readiness_report "
            "failed: %r", err,
        )
        return None


# ===========================================================================
# §33.1 — register_shipped_invariants
# ===========================================================================


def register_shipped_invariants() -> list:
    """Provider-readiness-gate substrate invariants.

    Four AST pins enforce the structural contract:
      1. Closed-7 ReadinessVerdict bytes-pinned
      2. Composes canonical claude_circuit_breaker + ClaudeProvider
         + DoublewordProvider — NO parallel state
      3. Authority asymmetry — gate is a pre-flight probe, not a
         decision authority
      4. Master flag default-FALSE per §33.1
    """
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/battle_test/"
        "provider_readiness_gate.py"
    )

    _FORBIDDEN_IMPORTS = (
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.iron_gate",
        "backend.core.ouroboros.governance.policy",
        "backend.core.ouroboros.governance.policy_engine",
        "backend.core.ouroboros.governance.candidate_generator",
        "backend.core.ouroboros.governance.urgency_router",
        "backend.core.ouroboros.governance.change_engine",
        "backend.core.ouroboros.governance.semantic_guardian",
        "backend.core.ouroboros.governance.auto_committer",
        "backend.core.ouroboros.governance.risk_tier_floor",
        "backend.core.ouroboros.governance.tool_executor",
        "backend.core.ouroboros.governance.plan_generator",
    )

    _EXPECTED_VERDICTS = frozenset({
        "ready",
        "disabled",
        "cb_open",
        "claude_probe_failed",
        "dw_probe_failed",
        "both_unhealthy",
        "error",
    })

    def _validate_verdict_taxonomy(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.ClassDef)
                and node.name == "ReadinessVerdict"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, _ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], _ast.Name)
                        and isinstance(sub.value, _ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                if found != _EXPECTED_VERDICTS:
                    return (
                        f"ReadinessVerdict drift: got="
                        f"{sorted(found)} expected="
                        f"{sorted(_EXPECTED_VERDICTS)}",
                    )
                return ()
        return ("ReadinessVerdict class not found",)

    def _validate_composes_canonical(
        tree: "_ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        for needle in (
            "claude_circuit_breaker",
            "get_claude_circuit_breaker",
            "ClaudeProvider",
            "DoublewordProvider",
            "health_probe",
            "should_allow_request",
        ):
            if needle not in source:
                violations.append(
                    f"must compose canonical {needle!r}"
                )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                if mod in _FORBIDDEN_IMPORTS:
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"forbidden import {mod!r}"
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in _ast.walk(node):
                    if (
                        isinstance(sub, _ast.Call)
                        and isinstance(sub.func, _ast.Name)
                        and sub.func.id == "_env_truthy"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(
                                    kw.value, _ast.Constant,
                                )
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled must call _env_truthy with "
                    "default=False per §33.1",
                )
        return ("master_enabled not found",)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "provider_readiness_gate_verdict_taxonomy"
            ),
            target_file=target,
            description=(
                "Closed-7 ReadinessVerdict bytes-pinned. New "
                "values require explicit scope doc + AST pin."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "provider_readiness_gate_composes_canonical"
            ),
            target_file=target,
            description=(
                "Composes claude_circuit_breaker + ClaudeProvider "
                "+ DoublewordProvider exclusively. NO parallel "
                "circuit breaker. NO parallel probe."
            ),
            validate=_validate_composes_canonical,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "provider_readiness_gate_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Gate MUST NOT import orchestrator / iron_gate / "
                "policy / etc. Pre-flight probe, not a decision "
                "authority."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "provider_readiness_gate_master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 cognitive substrate — master flag default-"
                "FALSE. Pre-existing battle-test behavior "
                "byte-identical until operator opts in."
            ),
            validate=_validate_master_default_false,
        ),
    ]


__all__ = [
    "PROVIDER_READINESS_GATE_SCHEMA_VERSION",
    "CircuitBreakerSnapshot",
    "ProbeResult",
    "ProviderReadinessReport",
    "ReadinessVerdict",
    "check_provider_readiness",
    "claude_probe_timeout_s",
    "dw_probe_timeout_s",
    "master_enabled",
    "register_shipped_invariants",
    "require_dw",
    "write_readiness_report",
]
