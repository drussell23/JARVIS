"""
VoiceCommandSensor (Sensor C) — Human voice intent → IntentEnvelope.

Called by the voice intent pipeline when a self-dev intent is recognized.
STT confidence gate: commands below threshold are flagged ``requires_human_ack=True``
so the router parks them for explicit confirmation before dispatch.

Rate guard: max ``rate_limit_per_hour`` voice-triggered ops per rolling 1-hour window.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, List

from backend.core.ouroboros.governance.operation_id import generate_operation_id
from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)

_SECONDS_PER_HOUR = 3600.0
_STOP_PHRASES = frozenset({"stop", "cancel", "abort", "halt"})


@dataclass
class VoiceCommandPayload:
    """Parsed voice command payload from the STT pipeline."""

    description: str
    target_files: List[str]
    repo: str
    stt_confidence: float = 1.0
    evidence: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.repo is None:
            raise ValueError("VoiceCommandPayload.repo must be a string, not None")


class VoiceCommandSensor:
    """Converts recognized voice self-dev commands into IntentEnvelopes.

    Parameters
    ----------
    router:
        UnifiedIntakeRouter.
    repo:
        Repository name.
    stt_confidence_threshold:
        STT confidence below this value → ``requires_human_ack=True``.
    rate_limit_per_hour:
        Maximum voice-triggered ops per rolling 1-hour window.
    """

    def __init__(
        self,
        router: Any,
        repo: str,
        stt_confidence_threshold: float = 0.82,
        rate_limit_per_hour: int = 3,
        signal_bus: Any = None,           # Optional[UserSignalBus]
    ) -> None:
        self._router = router
        self._repo = repo
        self._threshold = stt_confidence_threshold
        self._rate_limit = rate_limit_per_hour
        self._op_timestamps: List[float] = []
        self._signal_bus = signal_bus

    @staticmethod
    def _is_stop_command(description: str) -> bool:
        """Return True if the description contains a stop/cancel phrase."""
        words = description.lower().split()
        return any(w in _STOP_PHRASES for w in words)

    async def handle_voice_command(self, payload: VoiceCommandPayload) -> str:
        """Process one recognized voice command.

        Returns one of: ``"enqueued"``, ``"pending_ack"``,
        ``"rate_limited"``, ``"error"``, ``"stopped"``.
        """
        # GAP 6: detect stop/cancel commands and fire UserSignalBus
        if self._is_stop_command(payload.description):
            if self._signal_bus is not None:
                self._signal_bus.request_stop()
                logger.info("VoiceCommandSensor: stop command detected — bus.request_stop() fired")
                return "stopped"
            else:
                logger.warning("VoiceCommandSensor: stop command received but no signal_bus wired")
                return "error"

        if not payload.target_files:
            logger.warning("VoiceCommandSensor: empty target_files, skipping")
            return "error"

        # Rate limit: evict timestamps older than 1 hour
        now = time.monotonic()
        self._op_timestamps = [
            ts for ts in self._op_timestamps if (now - ts) < _SECONDS_PER_HOUR
        ]
        if len(self._op_timestamps) >= self._rate_limit:
            logger.info("VoiceCommandSensor: rate limit reached (%d/h)", self._rate_limit)
            return "rate_limited"

        # STT confidence gate
        requires_ack = payload.stt_confidence < self._threshold

        # causal_id == signal_id: the user voice command is the origin event
        origin_id = generate_operation_id("vox")
        evidence = dict(payload.evidence)
        evidence.setdefault("stt_confidence", payload.stt_confidence)
        evidence.setdefault("signature", payload.description[:64])

        envelope = make_envelope(
            source="voice_human",
            description=payload.description,
            target_files=tuple(payload.target_files),
            repo=payload.repo or self._repo,
            confidence=payload.stt_confidence,
            urgency="critical",
            evidence=evidence,
            requires_human_ack=requires_ack,
            causal_id=origin_id,
            signal_id=origin_id,
        )

        try:
            result = await self._router.ingest(envelope)
            if result in ("enqueued", "pending_ack"):
                self._op_timestamps.append(now)
            logger.info(
                "VoiceCommandSensor: result=%s requires_ack=%s cmd=%s",
                result, requires_ack, payload.description,
            )
            return result
        except Exception:
            logger.exception("VoiceCommandSensor: ingest failed: %s", payload.description)
            return "error"
