"""Battle-test provider-readiness pre-flight gate (P1).

Closes the v18 (bt-2026-05-16-175621) wasted-spend hole: a $2
discriminator soak launched into a live Anthropic 5xx/529 + DW-empty
window thrashed ~27-30 min producing no rubric signal. The
Fail-Fast Exhaustion CB (b07bb03965) makes such an op fail *fast*;
this gate goes one step earlier — **refuse to start the SWE inject
at all when providers are already known-bad, so $0 is spent**.

Pure composition (zero new stack — operator P1 mandate):

  * ``claude_circuit_breaker.get_claude_circuit_breaker()
    .should_allow_request()`` — the canonical "is the primary
    provider path healthy right now" signal. The breaker already
    tracks real-traffic 5xx/529 streaks (exactly the v18
    condition); OPEN ⇒ refuse. Free, synchronous, no request.
  * an optional bounded ``health_probe()`` (the caller passes a
    probe-able handle; composes the existing canonical coroutine) —
    an active liveness check on top of the passive CB state.

Note on hibernation: ``provider_exhaustion_watcher`` exposes no
clean process-wide "is-hibernating" accessor (only the class +
``hibernations_triggered()``), so this gate does NOT fabricate one.
Hibernation is downstream of the *same* provider-exhaustion streak
the Claude breaker already tracks — an OPEN CB is the authoritative
signal and covers that condition transitively. Composing only what
cleanly exists (honest > a dead read that always says "fine").

Contract: NEVER raises into the harness boot (boot-must-never-fail,
same as the existing SWE boot hook). Master flag §33.1
default-FALSE → byte-identical (no probe, no gate) when off.
Indeterminate (probe flaked/timed out) → Option (A) PROCEED+WARN by
default (a flaky probe must not shatter a legitimately-healthy paid
run; the Fail-Fast CB is the safety net behind A), env-tunable to
Option (B) strict-refuse for paranoid cost-protection.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("Ouroboros.BattleTest.ProviderPreflight")

PREFLIGHT_ENABLED_ENV_VAR: str = (
    "JARVIS_BATTLE_PREFLIGHT_PROVIDER_READINESS_ENABLED"
)
PREFLIGHT_STRICT_INDETERMINATE_ENV_VAR: str = (
    "JARVIS_BATTLE_PREFLIGHT_STRICT_INDETERMINATE"
)
PREFLIGHT_PROBE_TIMEOUT_ENV_VAR: str = (
    "JARVIS_BATTLE_PREFLIGHT_PROBE_TIMEOUT_S"
)
_DEFAULT_PROBE_TIMEOUT_S: float = 15.0


class PreflightVerdict(str, enum.Enum):
    """Closed taxonomy. ``PROCEED*`` → run the soak; ``REFUSE_*`` →
    skip the SWE inject before any spend."""

    PROCEED = "proceed"
    PROCEED_DISABLED = "proceed_disabled"          # flag off (legacy)
    PROCEED_INDETERMINATE_WARN = "proceed_indeterminate_warn"  # Opt A
    REFUSE_CLAUDE_CB_OPEN = "refuse_claude_cb_open"
    REFUSE_PROVIDER_UNREACHABLE = "refuse_provider_unreachable"

    @property
    def is_refusal(self) -> bool:
        return self.value.startswith("refuse_")


def preflight_enabled() -> bool:
    """§33.1 default-FALSE master switch. NEVER raises."""
    return os.environ.get(
        PREFLIGHT_ENABLED_ENV_VAR, "",
    ).strip().lower() in {"1", "true", "yes", "on"}


def preflight_strict_indeterminate() -> bool:
    """Option (B) toggle. Default FALSE = Option (A) PROCEED+WARN on
    an indeterminate probe (a flaky probe never blocks a healthy
    paid run — the Fail-Fast CB contains a genuinely-bad one). When
    TRUE, indeterminate → strict REFUSE (paranoid cost-protection).
    NEVER raises."""
    return os.environ.get(
        PREFLIGHT_STRICT_INDETERMINATE_ENV_VAR, "",
    ).strip().lower() in {"1", "true", "yes", "on"}


def _probe_timeout_s() -> float:
    try:
        v = float(os.environ.get(
            PREFLIGHT_PROBE_TIMEOUT_ENV_VAR, "",
        ).strip())
        return v if v > 0 else _DEFAULT_PROBE_TIMEOUT_S
    except (TypeError, ValueError):
        return _DEFAULT_PROBE_TIMEOUT_S


def _claude_cb_allows() -> Optional[bool]:
    """True/False from the canonical Claude breaker singleton;
    None if the breaker is unreachable (indeterminate). NEVER
    raises."""
    try:
        from backend.core.ouroboros.governance.claude_circuit_breaker import (  # noqa: E501
            get_claude_circuit_breaker,
        )
        return bool(get_claude_circuit_breaker().should_allow_request())
    except Exception:  # noqa: BLE001 — composition must be total
        return None


async def _active_health_probe(
    probe_handle: Any, timeout_s: float,
) -> Optional[bool]:
    """Bounded active liveness check composing the canonical
    ``health_probe()`` coroutine off the supplied handle. Returns
    True/False, or None when no handle / not awaitable / timeout /
    error (indeterminate). NEVER raises (CancelledError propagates).
    """
    fn = getattr(probe_handle, "health_probe", None)
    if fn is None or not asyncio.iscoroutinefunction(fn):
        return None  # no handle / not an async health_probe → indeterminate
    try:
        return bool(
            await asyncio.wait_for(fn(), timeout=max(0.1, timeout_s))
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — indeterminate, never fatal
        return None


async def assess_provider_readiness(
    *,
    probe_handle: Any = None,
) -> PreflightVerdict:
    """Single pre-spend admission gate. NEVER raises.

    Order: master-flag → Claude CB state (primary, authoritative;
    the v18 5xx/529 condition) → optional bounded active probe →
    indeterminate policy (A default PROCEED+WARN / B strict refuse)."""
    try:
        if not preflight_enabled():
            return PreflightVerdict.PROCEED_DISABLED

        cb = _claude_cb_allows()
        if cb is False:
            logger.error(
                "[ProviderPreflight] REFUSE: Claude circuit breaker "
                "is OPEN (recent 5xx/529 streak — the v18 outage "
                "condition; also covers the hibernation case "
                "transitively). Skipping SWE inject before any spend."
            )
            return PreflightVerdict.REFUSE_CLAUDE_CB_OPEN

        probe = await _active_health_probe(
            probe_handle, _probe_timeout_s(),
        )
        if probe is True:
            logger.info(
                "[ProviderPreflight] PROCEED: Claude CB closed + "
                "active health_probe OK."
            )
            return PreflightVerdict.PROCEED
        if probe is False:
            logger.error(
                "[ProviderPreflight] REFUSE: active health_probe "
                "returned unhealthy. Skipping SWE inject before any "
                "spend."
            )
            return PreflightVerdict.REFUSE_PROVIDER_UNREACHABLE

        # probe is None → indeterminate (no handle / timeout / flake)
        if cb is None and probe is None:
            # Nothing affirmatively healthy AND nothing affirmatively
            # bad — purely indeterminate.
            if preflight_strict_indeterminate():
                logger.error(
                    "[ProviderPreflight] REFUSE (strict): readiness "
                    "indeterminate (CB unreachable + no probe signal)."
                )
                return PreflightVerdict.REFUSE_PROVIDER_UNREACHABLE
            logger.warning(
                "[ProviderPreflight] PROCEED+WARN (Option A): "
                "readiness indeterminate; relying on the Fail-Fast "
                "Exhaustion CB to terminate fast if genuinely down."
            )
            return PreflightVerdict.PROCEED_INDETERMINATE_WARN

        # CB explicitly allowed (cb is True) but no/indeterminate
        # active probe → CB is an authoritative passive signal;
        # proceed (Option A) unless strict.
        if preflight_strict_indeterminate() and probe is None:
            logger.error(
                "[ProviderPreflight] REFUSE (strict): no active "
                "probe confirmation despite CB-closed."
            )
            return PreflightVerdict.REFUSE_PROVIDER_UNREACHABLE
        logger.info(
            "[ProviderPreflight] PROCEED: Claude CB closed "
            "(active probe indeterminate; CB is authoritative)."
        )
        return PreflightVerdict.PROCEED
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — gate NEVER fails boot
        logger.debug(
            "[ProviderPreflight] assess raised — Option A "
            "PROCEED+WARN (gate must never break boot)",
            exc_info=True,
        )
        return PreflightVerdict.PROCEED_INDETERMINATE_WARN


__all__ = [
    "PREFLIGHT_ENABLED_ENV_VAR",
    "PREFLIGHT_STRICT_INDETERMINATE_ENV_VAR",
    "PREFLIGHT_PROBE_TIMEOUT_ENV_VAR",
    "PreflightVerdict",
    "preflight_enabled",
    "preflight_strict_indeterminate",
    "assess_provider_readiness",
]
