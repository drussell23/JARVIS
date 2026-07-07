from __future__ import annotations
from backend.core.ouroboros.governance.comms.voice_build.classifier import (
    HeuristicClassifier, VoiceIntent,
)

def test_build_commands_classified_build():
    c = HeuristicClassifier()
    for t in ["add rate limiting to the auth endpoint",
              "fix the failing login test",
              "refactor the payment module"]:
        assert c.classify(t) == VoiceIntent.BUILD

def test_chat_and_noise_classified_ignore():
    c = HeuristicClassifier()
    for t in ["what time is it", "hey karen how are you", "", "   ", "thanks"]:
        assert c.classify(t) == VoiceIntent.IGNORE

def test_verb_set_is_injectable_not_hardcoded():
    c = HeuristicClassifier(build_verbs={"frobnicate"})
    assert c.classify("frobnicate the widget") == VoiceIntent.BUILD
    assert c.classify("add a feature") == VoiceIntent.IGNORE   # 'add' not in the custom set
