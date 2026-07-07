from __future__ import annotations

import enum
import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class Priority(enum.IntEnum):
    """Speech priority ladder. Higher preempts lower."""
    PROACTIVE_INFO = 1       # FYI narration
    PROACTIVE_CRITICAL = 2   # needs approval
    USER_RESPONSE = 3        # answer to a user command
    USER_BARGE_IN = 4        # user interrupting Karen


class VoiceState(str, enum.Enum):
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    KAREN_SPEAKING = "karen_speaking"
    THINKING = "thinking"


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    priority: Priority
    coalesce_key: str = ""   # same key → keep only the latest
    op_id: str = ""


@runtime_checkable
class PlaybackHandle(Protocol):
    """The audio floor. Sprint 3 wraps unified_voice_orchestrator; Sprint 1
    uses FakePlayback."""
    async def play(self, text: str) -> None: ...
    def preempt(self) -> None: ...
    @property
    def is_active(self) -> bool: ...


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


@dataclass(frozen=True)
class ArbiterConfig:
    enabled: bool = False
    barge_in_enabled: bool = False
    proactive_enabled: bool = False
    queue_max_per_priority: int = 8

    @classmethod
    def from_env(cls) -> "ArbiterConfig":
        return cls(
            enabled=_env_bool("JARVIS_KAREN_VOICE_ENABLED", False),
            barge_in_enabled=_env_bool("JARVIS_KAREN_BARGE_IN_ENABLED", False),
            proactive_enabled=_env_bool("JARVIS_KAREN_PROACTIVE_ENABLED", False),
            queue_max_per_priority=8,
        )
