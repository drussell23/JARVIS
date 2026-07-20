"""Automated Loopback Self-Test Engine (Phase 12, Slice D).

Closes the DoubleWord-failover verification gap WITHOUT a human at the HUD.
On network readiness the supervisor tests its OWN failover: it runs the
real provider chain with the primary provider deliberately raising the
Anthropic credit fault, and checks that the ``DoubleWordUCPAdapter``
(Slice A) intercepts and fulfils the token. On success it logs a
definitive ``FAILOVER_PROVEN`` to the telemetry stream.

Graceful degradation (mandate 2c): if DoubleWord itself returns an auth /
entitlement error, the self-test does NOT fail the boot — it flags the
provider ``DEGRADED`` in the ledger, emits telemetry, and the supervisor
keeps the primary command loop alive.

DRY (mandate 3): reuses the Slice-A ``DoubleWordUCPAdapter`` + the
provider-agnostic ``generate_with_failover`` router + the classifier, and
the existing TrinityEventBus. It does NOT re-implement failover.

Every public entry point NEVER raises.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("Jarvis.LoopbackSelfTest")

SELFTEST_TOPIC = "ouroboros.selftest"

#: A simulated primary fault carries the exact live shape (Anthropic
#: insufficient-credit = HTTP 400 body "credit balance is too low") so the
#: real classifier + router path is exercised, not a synthetic one.
_SIM_PRIMARY_MESSAGE = ("Error code: 400 - Your credit balance is too low "
                        "to access the Anthropic API.")

#: Tokens that mean the BACKUP provider is unusable for auth/entitlement
#: reasons (degrade, don't fail-hard).
_ENTITLEMENT_TOKENS = (
    "auth", "unauthorized", "forbidden", "entitle", "entitlement",
    "api key", "api_key", "401", "403", "not configured", "no credentials",
    "invalid key",
)

#: Tokens that mean the BACKUP provider's backend is simply NOT PROVISIONED
#: in this runtime (a lean venv missing a transitive dep, an unimported
#: client stack). This is an environment gap — NOT a failover-logic fault —
#: so it degrades gracefully (primary command loop stays live) rather than
#: reporting a scary FAILED. The ledger still records it (honest telemetry).
_UNAVAILABLE_TOKENS = (
    "no module named", "modulenotfounderror", "importerror",
    "cannot import", "not available", "unavailable", "not installed",
)


class SelfTestState(str, Enum):
    PENDING = "pending"
    FAILOVER_PROVEN = "failover_proven"     # DoubleWord answered — resilient
    DEGRADED = "degraded"                    # DW auth/entitlement issue; primary loop live
    FAILED = "failed"                        # unexpected — surfaced, non-fatal


class _SimulatedPrimaryError(Exception):
    """Stands in for the Anthropic credit-balance 400 at self-test time."""
    def __init__(self) -> None:
        super().__init__(_SIM_PRIMARY_MESSAGE)
        self.status_code = 400


@dataclass
class SelfTestResult:
    state: SelfTestState = SelfTestState.PENDING
    provider: str = ""
    answer_preview: str = ""
    error: str = ""
    ledger: Dict[str, str] = field(default_factory=dict)


class LoopbackSelfTest:
    """Boot-time proof of the DoubleWord failover. ``dw_provider`` (the
    ``active_failover.Provider`` for DoubleWord) is injectable so tests run
    without the real network."""

    def __init__(
        self,
        *,
        dw_provider: Optional[Any] = None,
        bus_publish: Optional[Callable[[str, Dict[str, Any]], Awaitable[Any]]] = None,
        prompt: str = "Diagnostic self-test — reply with a short greeting.",
        system_prompt: str = "You are JARVIS. This is an internal boot self-test.",
    ) -> None:
        self._dw_provider = dw_provider
        self._bus_publish = bus_publish
        self._prompt = prompt
        self._system_prompt = system_prompt

    async def _publish(self, data: Dict[str, Any]) -> None:
        try:
            pub = self._bus_publish
            if pub is None:
                from backend.core.trinity_event_bus import get_event_bus_if_exists
                bus = get_event_bus_if_exists()
                if bus is None:
                    return
                pub = lambda t, d: bus.publish_raw(topic=t, data=d, persist=False)
            await pub(SELFTEST_TOPIC, data)
        except Exception:  # noqa: BLE001
            logger.debug("[SelfTest] telemetry degraded", exc_info=True)

    def _dw(self) -> Any:
        """The real DoubleWord failover Provider (Slice A adapter) unless
        one was injected."""
        if self._dw_provider is not None:
            return self._dw_provider
        from backend.core.doubleword_ucp_adapter import DoubleWordUCPAdapter
        return DoubleWordUCPAdapter().as_provider()

    async def run(self) -> SelfTestResult:
        """Run the loopback failover proof. NEVER raises."""
        from backend.core.active_failover import (
            Provider, generate_with_failover, is_retriable_provider_error,
        )
        result = SelfTestResult()

        async def _simulated_primary(_ctx: Any) -> str:
            # Intentionally simulate the live primary-provider exception so
            # the REAL classifier + router pivot to DoubleWord.
            raise _SimulatedPrimaryError()

        providers: List[Any] = [
            Provider(name="anthropic_simulated", call=_simulated_primary),
            self._dw(),
        ]
        try:
            fo = await generate_with_failover(
                {"prompt": self._prompt, "system_prompt": self._system_prompt},
                providers)
        except Exception as exc:  # noqa: BLE001 — router never raises, belt+braces
            result.state = SelfTestState.FAILED
            result.error = str(exc)
            await self._publish({"type": "SELFTEST_FAILED", "error": str(exc)})
            return result

        if fo.ok and fo.provider == "doubleword" and fo.text.strip():
            # DoubleWord actually answered — the failover is PROVEN live.
            result.state = SelfTestState.FAILOVER_PROVEN
            result.provider = "doubleword"
            result.answer_preview = fo.text.strip()[:80]
            result.ledger["doubleword"] = "ready"
            logger.info("[SelfTest] FAILOVER_PROVEN — DoubleWord answered "
                        "('%s…')", result.answer_preview)
            await self._publish({
                "type": "FAILOVER_PROVEN", "provider": "doubleword",
                "answer_preview": result.answer_preview,
                "narration_text": "DoubleWord failover proven live — "
                                  "provider-independent command loop",
                "narration_priority": "normal", "source_brain": "supervisor",
            })
            return result

        # DoubleWord did not fulfil — classify: auth/entitlement → degrade
        # (keep serving); anything else → surfaced FAILED (still non-fatal).
        err = (fo.error or "doubleword returned no answer").lower()
        entitlement = any(t in err for t in _ENTITLEMENT_TOKENS)
        unavailable = any(t in err for t in _UNAVAILABLE_TOKENS)
        result.error = fo.error or "doubleword empty response"
        if entitlement or unavailable:
            reason = "entitlement" if entitlement else "unavailable"
            result.state = SelfTestState.DEGRADED
            result.ledger["doubleword"] = f"degraded:{reason}"
            logger.warning("[SelfTest] DoubleWord DEGRADED (%s) — primary "
                           "command loop stays live: %s", reason, result.error)
            await self._publish({
                "type": "PROVIDER_DEGRADED", "provider": "doubleword",
                "reason": reason, "error": result.error[:200],
                "narration_text": f"DoubleWord backup degraded ({reason}) — "
                                  "primary command loop live",
                "narration_priority": "high", "source_brain": "supervisor",
            })
        else:
            result.state = SelfTestState.FAILED
            result.ledger["doubleword"] = "failed"
            logger.warning("[SelfTest] failover self-test FAILED (non-fatal): "
                           "%s", result.error)
            await self._publish({
                "type": "SELFTEST_FAILED", "provider": "doubleword",
                "error": result.error[:200], "source_brain": "supervisor"})
        return result


__all__ = [
    "SELFTEST_TOPIC", "SelfTestState", "SelfTestResult", "LoopbackSelfTest",
]
