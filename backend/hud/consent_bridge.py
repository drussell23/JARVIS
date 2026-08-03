"""Ask the signed HUD for consent — and never wait for the answer.

`SecureConsent.swift` has been complete for a while: it parses a challenge,
prompts for Touch ID with a fresh `LAContext`, and echoes the nonce back as a
`consent_verdict`. `main.py` has been handling that verdict for just as long.
Between them sat nothing at all. Python never sent the question, so every gated
capability resolved to "no approval provider available — failing closed" and the
operator was never asked anything.

A gate that always denies looks identical to a gate that works, right up until
somebody tries to use the capability behind it. `lock_screen`,
`video.start_streaming`, `touch.watch_and_react` — all reachable, all named, all
refused.

WHY THIS RETURNS INSTEAD OF WAITING
-------------------------------------
`ApprovalProvider` also offers `await_decision`, and using it here would be the
obvious mistake. It blocks until a human answers, which outlives the provider
timeout, the route's generation budget, and the operator's patience, in that
order — and it holds the LLM's task open the whole time. So this only ASKS. The
router parks the call, the turn ends cleanly, and the verdict re-enters through
`resume` as a new turn. The whole suspension design depends on this function
being boring.

WHY A COUNT OF ZERO IS A DENIAL
---------------------------------
`publish` reports how many HUDs it reached. Zero means the question was never
asked — the HUD is not running, or the socket is not connected. Returning a
request id anyway would park a call that nothing can ever answer, and it would
sit there until its TTL looking exactly like a human who is thinking about it.
An unasked question is not a pending one.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("JARVIS.HUDConsent")

HUD_CONSENT_SCHEMA_VERSION: str = "hud_consent.v1"

#: The event the Swift side listens for. Must match the string
#: `BrainstemLauncher.processReceiveBuffer` dispatches on — a mismatch here is
#: silent on both sides, which is why it is a named constant rather than a
#: literal in the one place that sends it.
CONSENT_REQUEST_EVENT: str = "consent_request"


def hud_consent_enabled() -> bool:
    """Master gate. Default TRUE. NEVER raises.

    Off does not mean "approve everything" — it means no provider, which the
    router treats as failing closed. There is deliberately no setting that turns
    a gated capability into an ungated one.
    """
    return (os.environ.get("JARVIS_HUD_CONSENT_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


class HUDConsentProvider:
    """An `ApprovalProvider` that asks the signed HUD over the IPC. NEVER raises.

    Implements `request` only. `approve`, `reject` and `await_decision` are
    absent on purpose: the verdict arrives as an inbound IPC event and goes
    straight to `CapabilityRouter.resume`, so a second path for the same answer
    would be a second place for the nonce check to be forgotten.
    """

    def __init__(self, publish: Optional[Callable[[str, Dict[str, Any]], int]] = None) -> None:
        self._publish = publish
        self._asked = 0
        self._unreachable = 0

    def _publisher(self) -> Optional[Callable[[str, Dict[str, Any]], int]]:
        if self._publish is None:
            try:
                from backend.hud.ipc_server import publish
                self._publish = publish
            except Exception:  # noqa: BLE001
                logger.debug("[HUDConsent] no IPC publisher", exc_info=True)
        return self._publish

    async def request(self, ctx: Any) -> str:
        """Send the challenge and return its id. NEVER waits. NEVER raises.

        Returns "" when nothing was reached, which the router reads as a denial.
        """
        try:
            if not hud_consent_enabled():
                return ""
            publish = self._publisher()
            if publish is None:
                return ""
            request_id = uuid.uuid4().hex
            nonce = str(getattr(ctx, "nonce", "") or "")
            if not nonce:
                # `SecureConsent.Challenge` fails closed on an empty nonce, so a
                # challenge without one is a prompt the operator will never see.
                # Refusing here makes that a denial with a REASON rather than a
                # request that silently evaporates on the Swift side.
                logger.error("[HUDConsent] refusing to ask without a nonce — "
                             "the verdict could not be bound to the question")
                return ""
            reached = publish(CONSENT_REQUEST_EVENT, {
                "schema_version": HUD_CONSENT_SCHEMA_VERSION,
                "request_id": request_id,
                # The challenge the verdict must echo. Sent, never stored here —
                # the router owns it and compares in constant time.
                "nonce": nonce,
                "capability": str(getattr(ctx, "capability", "") or "unknown"),
                "detail": str(getattr(ctx, "description", "") or ""),
                "op_id": str(getattr(ctx, "op_id", "") or ""),
                # Lets the HUD say "this will keep running" in its own words
                # without having to know which capabilities are sessions.
                "session": str(getattr(ctx, "session", "") or ""),
            })
            if reached <= 0:
                self._unreachable += 1
                logger.warning(
                    "[HUDConsent] no HUD connected — '%s' cannot be approved "
                    "and will be denied. An unasked question is not a pending "
                    "one.", getattr(ctx, "capability", "?"))
                return ""
            self._asked += 1
            logger.info("[HUDConsent] asked %d HUD(s) to approve '%s' "
                        "(request=%s)", reached,
                        getattr(ctx, "capability", "?"), request_id[:12])
            return request_id
        except Exception:  # noqa: BLE001
            logger.debug("[HUDConsent] request degraded", exc_info=True)
            return ""

    def stats(self) -> Dict[str, Any]:
        try:
            from backend.hud.ipc_server import connected_clients
            connected = connected_clients()
        except Exception:  # noqa: BLE001
            connected = -1
        return {"schema_version": HUD_CONSENT_SCHEMA_VERSION,
                "enabled": hud_consent_enabled(), "asked": self._asked,
                "unreachable": self._unreachable, "hud_clients": connected}


def install(router: Any = None) -> bool:
    """Give the capability router a way to ask. Idempotent. NEVER raises.

    Called from the HUD boot. Without it the router's `_provider` is None and
    every gated capability denies — which is safe, and completely useless.
    """
    try:
        if not hud_consent_enabled():
            logger.info("[HUDConsent] disabled — gated capabilities will deny")
            return False
        if router is None:
            from backend.system_control.capability_router import (
                get_capability_router,
            )
            router = get_capability_router()
        if getattr(router, "_provider", None) is not None:
            return True
        router._provider = HUDConsentProvider()
        logger.info("[HUDConsent] installed — gated capabilities will now "
                    "prompt for Touch ID on the signed HUD")
        return True
    except Exception:  # noqa: BLE001
        logger.error("[HUDConsent] install failed — gated capabilities will "
                     "deny", exc_info=True)
        return False
