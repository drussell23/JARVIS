"""
barge_in_policy - when the user may interrupt JARVIS, and how an
interruption is told apart from JARVIS hearing itself.

The page that plays JARVIS's voice (the web app) keeps listening while it
speaks. What its recogniser hears then is either the user or JARVIS's own
voice coming back through the microphone -- over Remote Desktop, the remote
machine's speakers sit next to its microphone, and no echo canceller
stands between them. A transcript is JARVIS's echo when most of its words
are words JARVIS is saying; otherwise the user is talking, and JARVIS stops.

The policy is declared here, once, and served to the page (``/audio/barge-in``),
so the threshold that decides an interruption is the operator's setting and
not a number in a script. Read fresh on every request: a changed setting
applies to the next page load without a restart.

Settings (environment):
  JARVIS_BARGE_IN_ENABLED       -- the master switch, shared with the host's
                                   energy detector (backend/voice/barge_in_detector).
  JARVIS_BARGE_IN_MIN_WORDS     -- words heard before an interruption is
                                   judged at all (default one: "stop" is
                                   an interruption; raise it in a noisy room).
  JARVIS_BARGE_IN_ECHO_OVERLAP  -- the share of heard words found in what
                                   JARVIS is saying at or above which they
                                   are JARVIS's own echo.
  JARVIS_BARGEIN_COOLDOWN_MS    -- after an interruption, how long further
                                   ones are ignored (shared with
                                   backend/audio/barge_in_controller).
  JARVIS_BARGE_IN_ECHO_TAIL_MS  -- how long after JARVIS stops speaking its
                                   echo can still arrive: a line comes back
                                   through a remote session's microphone and
                                   the recogniser finalises it a second or
                                   two later, so "let me check" was heard as
                                   the user after the line had ended.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


@dataclass(frozen=True)
class BargeInPolicy:
    enabled: bool
    min_words: int
    echo_overlap: float
    cooldown_ms: int
    echo_tail_ms: int

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _setting(name: str, default: _T, cast: Callable[[str], _T], valid: Callable[[_T], bool]) -> _T:
    """*name* from the environment, or *default* when unset or invalid (a
    typo is logged and never breaks listening)."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
        if valid(value):
            return value
    except (TypeError, ValueError):
        pass
    logger.warning("[BargeIn] invalid %s=%r; using %r", name, raw, default)
    return default


def _flag(raw: str) -> bool:
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ValueError(raw)


def barge_in_policy() -> BargeInPolicy:
    """The policy as the operator has set it now."""
    return BargeInPolicy(
        enabled=_setting("JARVIS_BARGE_IN_ENABLED", True, _flag, lambda v: True),
        min_words=_setting("JARVIS_BARGE_IN_MIN_WORDS", 1, int, lambda v: v >= 1),
        echo_overlap=_setting("JARVIS_BARGE_IN_ECHO_OVERLAP", 0.6, float, lambda v: 0.0 < v <= 1.0),
        cooldown_ms=_setting("JARVIS_BARGEIN_COOLDOWN_MS", 500, int, lambda v: v >= 0),
        echo_tail_ms=_setting("JARVIS_BARGE_IN_ECHO_TAIL_MS", 2500, int, lambda v: v >= 0),
    )


__all__ = ["BargeInPolicy", "barge_in_policy"]
