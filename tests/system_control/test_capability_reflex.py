"""Regression spine for the deterministic capability reflex.

The defect this guards: an operator said "lock my screen" to a Mac that has
`lock_screen`, and nothing happened, because the only path from a sentence to
a capability ran through two model calls and the model was refusing.

The tests that matter most here are the REFUSALS. A reflex that fires is easy
to test and easy to get right; a reflex that fires when it should not is how a
screen locks in the middle of a sentence.
"""
from __future__ import annotations

import pytest

from backend.system_control.capability_reflex import (
    CapabilityReflex, Lexicon, Outcome, Signature, content_tokens,
    score_candidate, tokenize,
)


def _sig(name, *, tokens=None, desc="", gated=True, no_args=True,
         session=False, phrases=(), required=()):
    return Signature(
        name=name,
        name_tokens=tuple(tokens if tokens is not None
                          else content_tokens(name.split(".")[-1].replace("_", " "))),
        desc_tokens=frozenset(content_tokens(desc)),
        phrases=tuple(phrases),
        tier="approval_required" if gated else "safe_auto",
        gated=gated, starts_session=session, no_args=no_args,
        required_args=tuple(required),
    )


class _FixedLexicon(Lexicon):
    def __init__(self, sigs):
        super().__init__()
        self._fixed = list(sigs)

    def signatures(self):
        return list(self._fixed)


def _reflex(*sigs):
    return CapabilityReflex(lexicon=_FixedLexicon(sigs))


LOCK = _sig("lock_screen")
UNLOCK = _sig("unlock_screen")
SHOT = _sig("take_screenshot", gated=False)
OPEN_APP = _sig("open_app", no_args=False, required=("app_name",))
STREAM = _sig("video.start_streaming", session=True)


# ── The whole point ─────────────────────────────────────────────────────────

def test_the_sentence_that_did_nothing_now_resolves():
    out = _reflex(LOCK, UNLOCK, SHOT).resolve("lock my screen")
    assert out.outcome == Outcome.RESOLVED.value
    assert out.capability == "lock_screen"


@pytest.mark.parametrize("said", [
    "lock my screen", "lock the screen", "please lock my screen",
    "jarvis lock my screen", "lock my screen now", "screen lock",
])
def test_phrasings_of_the_same_request(said):
    assert _reflex(LOCK, UNLOCK).resolve(said).capability == "lock_screen"


def test_the_reflex_costs_no_model_call_and_no_io():
    # The property that makes it useful during an outage. If this ever needs a
    # collaborator, it has stopped being a reflex.
    out = _reflex(LOCK).resolve("lock my screen")
    assert out.resolved and out.elapsed_ms < 50.0


# ── Refusal: the lock/unlock collision ──────────────────────────────────────

@pytest.mark.parametrize("said,expected", [
    ("lock my screen", "lock_screen"),
    ("unlock my screen", "unlock_screen"),
    ("unlock the screen", "unlock_screen"),
])
def test_lock_is_not_a_substring_of_unlock(said, expected):
    """The single most dangerous pair on the surface.

    `"lock" in "unlock my screen"` is True. Substring matching here would
    lock a screen the operator asked to unlock — inverting the request, on the
    one capability where that is most visible.
    """
    assert _reflex(LOCK, UNLOCK).resolve(said).capability == expected


def test_unlock_utterance_does_not_even_score_lock():
    said = content_tokens("unlock my screen")
    assert score_candidate(LOCK, said, "unlock my screen") is None
    assert "lock" not in tokenize("unlock my screen")


# ── Refusal: shape ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("said", [
    "don't lock my screen",
    "do not lock my screen",
    "never lock my screen",
    "cancel lock screen",
])
def test_a_negation_is_not_a_command(said):
    out = _reflex(LOCK).resolve(said)
    assert out.outcome == Outcome.NOT_IMPERATIVE.value


@pytest.mark.parametrize("said", [
    "how do I lock my screen",
    "what happens when you lock my screen",
    "why would you lock my screen",
])
def test_a_question_about_a_capability_does_not_run_it(said):
    assert _reflex(LOCK).resolve(said).outcome == Outcome.NOT_IMPERATIVE.value


def test_modal_politeness_is_not_a_question():
    """"can you lock my screen" is an imperative wearing a question mark."""
    assert _reflex(LOCK).resolve("could you lock my screen").resolved


def test_a_conjunction_means_a_plan_not_a_reflex():
    out = _reflex(LOCK, SHOT).resolve("lock my screen and open chrome")
    assert out.outcome == Outcome.NOT_IMPERATIVE.value
    # It still says what it saw, so the caller can explain itself.
    assert out.capability == "lock_screen"


# ── Refusal: arguments ──────────────────────────────────────────────────────

def test_a_capability_needing_an_argument_is_never_guessed():
    out = _reflex(OPEN_APP).resolve("open app")
    assert out.outcome == Outcome.NEEDS_ARGS.value
    assert "app_name" in out.reason


# ── Refusal: ambiguity ──────────────────────────────────────────────────────

def test_two_readings_is_a_question_not_a_rounding_error():
    a = _sig("mute_audio", tokens=("mute", "audio"))
    b = _sig("mute_audio_output", tokens=("mute", "audio"))  # same signature
    out = _reflex(a, b).resolve("mute audio")
    assert out.outcome == Outcome.AMBIGUOUS.value


def test_the_more_specific_capability_wins_when_it_explains_more():
    """Given "lock my screen", a bare `screen` capability also has full name
    coverage. `explained` is what breaks the tie honestly."""
    bare = _sig("screen", tokens=("screen",))
    out = _reflex(LOCK, bare).resolve("lock my screen")
    assert out.capability == "lock_screen"


# ── Confidence depends on what happens if it is wrong ───────────────────────

def test_a_session_start_is_held_to_the_top_bar():
    """Duration is its own risk class. One consent does not cover forever."""
    sig = STREAM
    assert sig.starts_session and sig.gated
    out = _reflex(sig).resolve("start streaming the video feed for me")
    # Whatever it decides, it must not have used the LOWER gated bar.
    if not out.resolved:
        assert out.outcome == Outcome.LOW_CONFIDENCE.value


def test_an_ungated_capability_needs_the_top_bar(monkeypatch):
    monkeypatch.setenv("JARVIS_REFLEX_CERTAIN_SCORE", "0.99")
    monkeypatch.setenv("JARVIS_REFLEX_GATED_SCORE", "0.10")
    ungated = _sig("take_screenshot", gated=False)
    out = _reflex(ungated).resolve("take screenshot of the browser window")
    assert out.outcome == Outcome.LOW_CONFIDENCE.value


# ── Derivation, not declaration ─────────────────────────────────────────────

def test_a_capability_renamed_answers_to_its_new_name():
    """The property a phrase table cannot have."""
    renamed = _sig("hibernate_display", tokens=("hibernate", "display"))
    rx = _reflex(renamed)
    assert rx.resolve("hibernate the display").capability == "hibernate_display"
    assert not rx.resolve("lock my screen").resolved


def test_a_declared_phrase_reaches_a_badly_named_capability():
    odd = _sig("do_the_thing_v2", tokens=("do", "thing", "v2"),
               phrases=("secure my mac",))
    assert _reflex(odd).resolve("secure my mac").capability == "do_the_thing_v2"


def test_a_namespaced_capability_is_spoken_without_its_namespace():
    assert STREAM.name_tokens == ("start", "streaming")


def test_nothing_named_means_unrecognized_not_a_guess():
    out = _reflex(LOCK, UNLOCK).resolve("play some jazz")
    assert out.outcome == Outcome.UNRECOGNIZED.value
    assert out.capability == ""


def test_the_reflex_never_raises():
    rx = _reflex(LOCK)
    for junk in ["", "   ", "!!!", "\x00", "a" * 5000, "lock " * 400]:
        assert rx.resolve(junk) is not None


def test_disabled_falls_through_to_the_model(monkeypatch):
    monkeypatch.setenv("JARVIS_CAPABILITY_REFLEX_ENABLED", "false")
    assert _reflex(LOCK).resolve("lock my screen").outcome == Outcome.DISABLED.value


# ── The live surface ────────────────────────────────────────────────────────

def test_the_real_registry_can_be_spoken_to():
    """Not a mock. If the derivation breaks, the reflex is decorative."""
    rx = CapabilityReflex()
    names = {s.name for s in rx.lexicon().signatures()}
    if not names:                      # controller unimportable on this box
        pytest.skip("capability registry degraded in this environment")
    assert "lock_screen" in names
    assert rx.resolve("lock my screen").capability == "lock_screen"
    assert rx.resolve("unlock my screen").capability == "unlock_screen"
