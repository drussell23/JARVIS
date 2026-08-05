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

    THIS CHANNEL HAS A PRECONDITION
    ---------------------------------
    `requires` is the world state this channel needs in order to be ANSWERABLE
    at all. Touch ID draws a system dialog, and there is no surface to draw on
    while the screen is locked.

    That is not a theoretical concern. Measured on 2026-08-03 at 00:18:58, an
    operator said "unlock my screen":

        [HUD] Voice result: awaiting-consent
        [Brainstem] sendEvent: consent_verdict (249 bytes)      +99ms
        [HUD] consent verdict for unlock_screen → denied (operator declined)

    Ninety-nine milliseconds. No human declined anything — `canEvaluatePolicy`
    failed instantly because the screen was locked, and the router rendered it
    as a refusal the operator does not remember giving.

    So `unlock_screen` could never be authorised through this channel, ever, by
    construction: **a gate whose only answer channel requires the very state
    the gated action exists to produce is a deadlock, not a safeguard.**

    Declaring the precondition lets `CapabilityRouter` see that before it
    suspends, and refuse with the real reason instead of parking a call nothing
    can answer.

    Implements `request` only. `approve`, `reject` and `await_decision` are
    absent on purpose: the verdict arrives as an inbound IPC event and goes
    straight to `CapabilityRouter.resume`, so a second path for the same answer
    would be a second place for the nonce check to be forgotten.
    """

    #: World-state predicates that must hold for this channel to reach a human.
    requires = ("screen_unlocked",)

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


class VoiceConsentProvider:
    """Consent by speaker verification. Reachable through a locked screen.

    NEVER raises. The counterpart to `HUDConsentProvider`: same interface, no
    `requires`, because a microphone does not need a screen to be unlocked.

    IMMEDIATE, NOT SUSPENDED
    --------------------------
    Touch ID suspends because a human takes an unbounded amount of time to
    answer. This does not: the operator ALREADY answered — they spoke, and the
    evidence was captured with the command. Verification is a local
    computation on bytes we hold, so it resolves inside the turn.

    That is why this returns a decision rather than a request id, and why
    `CapabilityRouter` treats it as a distinct kind of authority. Suspending on
    it would park a call waiting for a human who has nothing left to do.
    """

    #: Nothing. A microphone works through a locked screen — that is the whole
    #: reason this authority exists.
    requires: tuple = ()
    immediate: bool = True

    def __init__(self) -> None:
        self._verified = 0
        self._refused = 0

    async def decide(self, ctx: Any) -> Any:
        """Verify the held utterance and answer. NEVER raises."""
        try:
            from backend.hud.utterance_audio import get_utterance_holder
            from backend.hud.voice_identity import Readiness, get_voice_identity

            # DO NOT SPEND THE EVIDENCE ON AN ATTEMPT THAT CANNOT SUCCEED.
            #
            # Measured live 2026-08-04 19:10:13, saying "unlock my screen":
            #
            #   [VoiceRouter] CAI: 'screen_unlocked' unmet -> running
            #                 'unlock_screen' first
            #   [VoiceConsent] 'unlock_screen' -> not_ready (model cold;
            #                  enrollment Derek J. Russell)
            #   [CapabilityRouter] 'unlock_screen' NOT authorised (I'm still
            #                  loading my voice recognition -- give me a
            #                  moment and ask again.)
            #   [VoiceIdentity] speaker model READY after 2.8s
            #
            # Every part of that is correct except the order. The model is
            # lazy on purpose — warming it at boot was the 14s deafness — so
            # the FIRST verification of a session is guaranteed to report
            # NOT_READY, because it is the thing that starts the load.
            #
            # But `claim()` is destructive, and it ran first. So the one
            # attempt that could never succeed was also the one that deleted
            # the sentence, and the invitation to "ask again" required the
            # operator to say it again — while the model became ready 2.8
            # seconds later, holding nothing.
            #
            # Claim only when a verdict is actually reachable; otherwise look
            # without taking. Anti-replay is untouched: verification still
            # consumes, and nothing verifies from a peek.
            svc = get_voice_identity()
            holder = get_utterance_holder()
            if svc.readiness is Readiness.READY:
                held = holder.claim()
            else:
                held = holder.peek()
            ident = await svc.identify(
                held.audio if held else None,
                sample=held.digest if held else "")
            if ident.approves:
                self._verified += 1
            else:
                self._refused += 1
            logger.info("[VoiceConsent] '%s' → %s (%s)",
                        getattr(ctx, "capability", "?"), ident.verdict,
                        ident.detail or "-")
            return ident
        except Exception as exc:  # noqa: BLE001 — a fault is never consent
            logger.error("[VoiceConsent] degraded: %s", exc)
            return None

    def stats(self) -> Dict[str, Any]:
        try:
            from backend.hud.voice_identity import get_voice_identity
            v = get_voice_identity().stats()
        except Exception:  # noqa: BLE001
            v = {}
        return {"verified": self._verified, "refused": self._refused,
                "identity": v}


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

        # The authority that answers when Touch ID cannot.
        #
        # Registered as a FALLBACK, never a replacement: the router consults it
        # only after finding the primary channel unreachable, so while a prompt
        # could have been shown to the operator it still is. Without this,
        # `unlock_screen` is a gate that can only ever refuse itself.
        #
        # Warming starts here rather than at first use because the model took
        # over 150 seconds to load when measured, and the utterance it would
        # verify expires in thirty.
        try:
            import asyncio as _aio
            router.add_authority(VoiceConsentProvider())
            from backend.hud.voice_identity import get_voice_identity
            ident = get_voice_identity()
            # `warm()` rather than `start_warming()`: enrollment is a single
            # database row and resolves in milliseconds, while the speaker
            # model takes minutes. Asking both at boot means the organism knows
            # WHO it would be checking for long before it can check — which is
            # the difference between "give me a moment" and "I don't know you".
            _aio.get_event_loop().create_task(ident.warm())
            logger.info("[HUDConsent] voice authority installed — reachable "
                        "through a locked screen; enrollment + speaker model "
                        "resolving in the background")
        except Exception as _va:  # noqa: BLE001
            logger.error("[HUDConsent] voice authority unavailable: %s", _va)
        return True
    except Exception:  # noqa: BLE001
        logger.error("[HUDConsent] install failed — gated capabilities will "
                     "deny", exc_info=True)
        return False
