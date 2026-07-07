from __future__ import annotations
import enum
from typing import Iterable, Optional, Protocol

class VoiceIntent(enum.Enum):
    BUILD = "build"
    IGNORE = "ignore"

class VoiceIntentClassifier(Protocol):
    def classify(self, text: str) -> VoiceIntent: ...

_DEFAULT_BUILD_VERBS = frozenset({
    "add", "fix", "refactor", "implement", "build", "create", "remove",
    "delete", "rename", "update", "change", "write", "make", "wire", "harden",
})

class HeuristicClassifier:
    """Deterministic build-intent detection. The verb set is injectable (not a
    frozen module constant) so the policy is configurable, not hardcoded; an
    LLM-backed classifier can implement the same protocol later."""
    def __init__(self, build_verbs: Optional[Iterable[str]] = None) -> None:
        self._verbs = frozenset(v.lower() for v in (build_verbs or _DEFAULT_BUILD_VERBS))
    def classify(self, text: str) -> VoiceIntent:
        if not text or not text.strip():
            return VoiceIntent.IGNORE
        words = text.strip().lower().split()
        # imperative: a build verb in the first two tokens
        if any(w.strip(".,!?") in self._verbs for w in words[:2]):
            return VoiceIntent.BUILD
        return VoiceIntent.IGNORE
