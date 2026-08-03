"""Tell the HUD when JARVIS is speaking — over the socket, not a lockfile.

There is already a complete authority on this. `UnifiedSpeechStateManager`
tracks who is speaking, from which of seven sources, with a length-scaled echo
cooldown, a text-similarity check that catches an echo the gate missed, an
`AudioBus` mic gate, and a 60-second watchdog that resets `is_speaking` if a
`stop` never arrives. It has been right for a long time.

The HUD — which owns the actual microphone — was not connected to any of it.
`main.py::_hud_tts` skipped the manager entirely and touched
`/tmp/jarvis_speaking`, and `WakeWordListener` read that file. Meanwhile the
manager's own `register_websocket_broadcaster` had ZERO callers, so the
broadcast surface built for precisely this was dark.

This module is the missing wire, and deliberately nothing else: no second
notion of "speaking", no parallel cooldown, no new watchdog. It subscribes to
the authority and forwards to the transport.

WHY THE SOCKET AND NOT THE FILE
---------------------------------
A lockfile records a FACT with no LIVENESS. If Python is SIGKILLed mid-utterance
the file survives, and `finally:` does not run — the HUD stays deaf until
somebody notices and deletes it by hand. That is not a hypothetical; it is the
same orphaned-state class as a leaked capability lease and an abandoned virtual
display.

The IPC socket has the property the file cannot: when the process dies the
connection drops, and the HUD is told. Crash-safety stops being a cleanup
routine somebody has to remember and becomes a consequence of the transport.
The channel already exists — `ipc_server.publish` was built to carry consent
challenges — so this adds a message type, not a mechanism.

EVERY FRAME CARRIES A DEADLINE
--------------------------------
A mute is a claim on the operator's microphone, and no claim on a microphone
should be able to outlive its own plausible duration. Each `speaking` frame
therefore says WHEN it stops being true, computed from the estimated utterance
length plus the manager's own cooldown. If the socket dies, if a broadcast is
dropped, if this process is killed between start and stop — the HUD's claim
expires on its own clock and the mic comes back.

Belt and braces with the same rule as elsewhere in this codebase: the reaper
must not depend on the thing it guards.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("JARVIS.HUDSpeechBridge")

SPEECH_BRIDGE_SCHEMA_VERSION: str = "hud_speech_bridge.v1"

#: The event the Swift side listens for. Must match the string
#: `BrainstemLauncher.dispatchInbound` switches on — a mismatch is silent on
#: both sides, so it is a named constant rather than a literal at the one place
#: that sends it.
SPEECH_STATE_EVENT: str = "speech_state"


def speech_bridge_enabled() -> bool:
    """Master gate. Default TRUE. NEVER raises.

    Off means the HUD is not told, which means it does not mute — the mic stays
    live and JARVIS may hear itself. That is the LOUD failure, and it is the
    right one: the alternative default would be a silently deaf microphone.
    """
    return (os.environ.get("JARVIS_HUD_SPEECH_BRIDGE_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def speaking_grace_ms() -> float:
    """Extra head-room added to every deadline. Clamped. NEVER raises.

    Covers the gap between "this process believes speech ended" and "the last
    sample has actually left the speaker". Too small and the tail of an
    utterance re-enters the mic; too large and the operator is briefly unheard
    after JARVIS finishes. The manager's own cooldown already models the echo
    tail, so this only absorbs scheduling jitter.
    """
    try:
        return max(0.0, min(float(os.environ.get(
            "JARVIS_HUD_SPEECH_GRACE_MS", "400")), 5000.0))
    except (TypeError, ValueError):
        return 400.0


def max_claim_ms() -> float:
    """The longest a single mute claim may last. Clamped. NEVER raises.

    The ceiling that makes permanent deafness impossible rather than unlikely.
    Mirrors `SpeechStateConfig.MAX_SPEAKING_DURATION_MS` — the manager already
    decided no single utterance runs past 60s, and a mute derived from it must
    not outlive the thing it was derived from.
    """
    try:
        return max(1000.0, min(float(os.environ.get(
            "JARVIS_HUD_SPEECH_MAX_CLAIM_MS", "60000")), 300000.0))
    except (TypeError, ValueError):
        return 60000.0


def estimate_speech_ms(text: str) -> float:
    """How long this utterance will plausibly take to say. NEVER raises.

    Deliberately an OVER-estimate. Under-estimating unmutes while JARVIS is
    still talking, which feeds the loop this exists to break; over-estimating
    costs a fraction of a second of not being heard. The asymmetry is the whole
    reason this is not a tight fit.

    ~12 characters/second is a slow-ish conversational rate, which is what the
    HUD's own utterances use (`rate = 0.52`, below AVSpeechUtterance's default).
    """
    try:
        chars = len(text or "")
        return min(1500.0 + (chars / 12.0) * 1000.0, max_claim_ms())
    except Exception:  # noqa: BLE001
        return 5000.0


def build_frame(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn one manager broadcast into a HUD frame. NEVER raises.

    Returns None for anything that is not a speech-state change, so an
    unrelated broadcaster message can never mute a microphone.

    The frame is FLAT and absolute: a boolean and a wall-clock deadline. The
    HUD is not asked to re-derive anything from durations or to track cooldown
    itself — two clocks agreeing on a rule is how they eventually disagree.
    """
    try:
        if not isinstance(message, dict):
            return None
        if message.get("type") != "speech_state_change":
            return None
        state = message.get("state")
        # `or {}` here used to turn a MISSING state into an empty one, which
        # then read as "not speaking" and produced a perfectly well-formed
        # unmute frame from a message that said nothing at all. Unmuting is the
        # safe direction, but fabricating a conclusion from absent data is how a
        # gate stops being trustworthy in the other direction later.
        if not isinstance(state, dict) or "is_speaking" not in state:
            return None

        speaking = bool(state.get("is_speaking"))
        now_ms = time.time() * 1000.0

        if speaking:
            text = str(state.get("current_text") or "")
            # Prefer the manager's own numbers where it has them; only
            # estimate what it does not track.
            started = state.get("speech_started_at")
            base = float(started) if isinstance(started, (int, float)) else now_ms
            deadline = base + estimate_speech_ms(text) + speaking_grace_ms()
        else:
            # NOT simply "unmute now". The manager keeps a cooldown after speech
            # ends precisely because the room is still ringing; honouring it is
            # what stops the tail of JARVIS's own sentence being transcribed as
            # a command.
            cooldown_until = state.get("cooldown_until")
            deadline = (float(cooldown_until)
                        if isinstance(cooldown_until, (int, float))
                        and float(cooldown_until) > now_ms
                        else now_ms)

        # Hard ceiling, applied last so no arithmetic above can escape it.
        deadline = min(deadline, now_ms + max_claim_ms())

        return {
            "schema_version": SPEECH_BRIDGE_SCHEMA_VERSION,
            "speaking": speaking,
            # Absolute, milliseconds since epoch. The HUD converts once, on
            # arrival, against its own monotonic clock — see SpeechGate.
            "deadline_ms": deadline,
            "now_ms": now_ms,
            "source": str(state.get("current_source") or "unknown"),
            "in_cooldown": bool(state.get("in_cooldown")),
            "event": str(message.get("event") or ""),
        }
    except Exception:  # noqa: BLE001
        logger.debug("[SpeechBridge] frame build degraded", exc_info=True)
        return None


class HUDSpeechBroadcaster:
    """Forwards manager broadcasts to the HUD. NEVER raises.

    A callable, because that is the shape `register_websocket_broadcaster`
    accepts. Stateful only for counters — the authority is the manager and this
    holds no opinion of its own about whether JARVIS is speaking.
    """

    def __init__(self, publish: Optional[Callable[[str, Dict[str, Any]], int]] = None) -> None:
        self._publish = publish
        self._sent = 0
        self._unreached = 0
        self._last: Dict[str, Any] = {}

    def _publisher(self) -> Optional[Callable[[str, Dict[str, Any]], int]]:
        if self._publish is None:
            try:
                from backend.hud.ipc_server import publish
                self._publish = publish
            except Exception:  # noqa: BLE001
                logger.debug("[SpeechBridge] no IPC publisher", exc_info=True)
        return self._publish

    def __call__(self, message: Dict[str, Any]) -> None:
        """Called by the manager on every state change. NEVER raises."""
        try:
            if not speech_bridge_enabled():
                return
            frame = build_frame(message)
            if frame is None:
                return
            publish = self._publisher()
            if publish is None:
                return
            reached = publish(SPEECH_STATE_EVENT, frame)
            self._last = frame
            if reached > 0:
                self._sent += 1
            else:
                self._unreached += 1
                # NOT an error. No HUD connected means no microphone of ours is
                # listening, so there is nothing to mute and nothing to warn
                # about. Counted so the difference is visible.
                logger.debug("[SpeechBridge] no HUD connected for %s",
                             frame.get("event"))
        except Exception:  # noqa: BLE001
            logger.debug("[SpeechBridge] broadcast degraded", exc_info=True)

    def stats(self) -> Dict[str, Any]:
        return {"schema_version": SPEECH_BRIDGE_SCHEMA_VERSION,
                "enabled": speech_bridge_enabled(), "sent": self._sent,
                "unreached": self._unreached, "last": dict(self._last)}


_BROADCASTER: Optional[HUDSpeechBroadcaster] = None


async def install() -> bool:
    """Subscribe the HUD to the speech authority. Idempotent. NEVER raises.

    Called once from the HUD boot. Without it the manager keeps tracking speech
    perfectly and the HUD keeps never hearing about it — which is the state this
    module was written to end, and which looked exactly like working software
    because both halves were individually fine.
    """
    global _BROADCASTER
    try:
        if not speech_bridge_enabled():
            logger.info("[SpeechBridge] disabled — the HUD will NOT mute its "
                        "microphone while JARVIS speaks")
            return False
        if _BROADCASTER is not None:
            return True
        from backend.core.unified_speech_state import get_speech_state_manager

        manager = await get_speech_state_manager()
        broadcaster = HUDSpeechBroadcaster()
        manager.register_websocket_broadcaster(broadcaster)
        _BROADCASTER = broadcaster
        logger.info("[SpeechBridge] installed — HUD mic now follows "
                    "UnifiedSpeechStateManager over the IPC socket")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("[SpeechBridge] install failed (%s) — the HUD will not "
                     "mute while JARVIS speaks", exc, exc_info=True)
        return False


def get_broadcaster() -> Optional[HUDSpeechBroadcaster]:
    """Testing / observability seam. NEVER raises."""
    return _BROADCASTER


def reset() -> None:
    """Testing seam. NEVER raises."""
    global _BROADCASTER
    _BROADCASTER = None
