"""A misheard word must not launch an application.

PRIORITY 1 of the intelligent command handler was a bare substring scan:

    any(word in text.lower() for word in
        ['weather', ..., 'rain', 'snow', 'hot', 'cold', 'storm'])

`in` on a string matches ANYWHERE, so short common words matched inside other
words — ahead of every real classifier — and the handler opened the Weather
app. On phrases this system produces constantly:

    "the brain is thinking"  -> 'rain'  -> Weather app
    "training the model"     -> 'rain'  -> Weather app
    "I took a shot"          -> 'hot'   -> Weather app
    "scold the agent"        -> 'cold'  -> Weather app

A speech-recognition artifact reaches this line too, so a misheard fragment
could launch an application unbidden. That is the part that matters: taking
an action on someone's machine because a substring appeared.
"""

from __future__ import annotations

import pytest

from backend.voice.intelligent_command_handler import _is_weather_query


# ---------------------------------------------------------------------------
# The regression — words this system says constantly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", [
    "the brain is thinking",            # 'rain' — an AI system's favourite word
    "training the model",               # 'rain'
    "retraining the classifier",        # 'rain'
    "I took a shot at it",              # 'hot'
    "hotfix deployed",                  # 'hot'
    "scold the agent",                  # 'cold'
    "the cold start took 12 seconds",   # 'cold', and a real sentence from here
    "snowflake schema migration",       # 'snow'
    "brainstorm the approach",          # 'rain' AND 'storm'
    "hello Karen",
    "what are you working on right now?",
])
def test_ordinary_speech_does_not_launch_the_weather_app(phrase):
    assert _is_weather_query(phrase) is False, (
        f"{phrase!r} would open the Weather app"
    )


def test_the_substring_scan_would_have_failed_these():
    """Proves these cases are real regressions rather than decoration — the
    OLD predicate is reconstructed and must disagree."""
    old_words = ['weather', 'temperature', 'forecast', 'rain', 'snow', 'sunny',
                 'cloudy', 'hot', 'cold', 'humid', 'windy', 'storm']

    def _old(text):
        return any(w in text.lower() for w in old_words)

    for phrase in ("the brain is thinking", "training the model",
                   "hotfix deployed", "scold the agent"):
        assert _old(phrase) is True, "test no longer reproduces the old bug"
        assert _is_weather_query(phrase) is False


# ---------------------------------------------------------------------------
# Real weather queries still route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", [
    "what's the weather",
    "what is the forecast today",
    "check the temperature",
    "what's the humidity in there",
    "is it cold outside",
    "how hot is it",
    "will it rain later",
    "is it going to snow",
    "are there storms coming",
])
def test_genuine_weather_questions_still_route(phrase):
    assert _is_weather_query(phrase) is True, f"{phrase!r} lost its route"


def test_unambiguous_words_need_no_question_shape():
    """"weather" means weather. It does not need an interrogative around it."""
    assert _is_weather_query("weather") is True
    assert _is_weather_query("pull up the forecast") is True


def test_ambiguous_words_require_interrogative_context():
    """The distinction the fix rests on: the SAME word, asking versus stating."""
    assert _is_weather_query("is it cold outside") is True
    assert _is_weather_query("the cold start took 12 seconds") is False


def test_word_boundaries_are_enforced():
    for embedded in ("brain", "training", "shot", "scold", "snowflake"):
        assert _is_weather_query(embedded) is False


def test_the_predicate_never_raises():
    for junk in (None, 123, object(), ""):
        assert _is_weather_query(junk) in (True, False)


def test_the_handler_uses_the_predicate_not_a_substring_scan():
    """Structural pin. The scan is a one-line edit away from returning, and
    nothing at runtime would flag it — it fails OPEN, into an app launch."""
    from pathlib import Path

    src = Path(
        "backend/voice/intelligent_command_handler.py",
    ).read_text(encoding="utf-8")
    assert "_is_weather_query(text)" in src
    assert "for word in ['weather'" not in src, "the substring scan is back"


# ---------------------------------------------------------------------------
# The SECOND matcher — the one three modules actually route through
# ---------------------------------------------------------------------------
#
# `weather_bridge_unified.is_weather_query` had the same substring bug with a
# worse keyword list, and it sits directly upstream of
# subprocess.run(['open', '-a', 'Weather']). Fixing only the command handler
# left this one reachable, and the Weather app kept opening.
#
#     "close the window"        -> 'wind'
#     "open a new window"       -> 'wind'
#     "the model is warming up" -> 'warm'


def _canonical():
    """Load the predicate without the package's import side-effects."""
    import ast as _ast
    import re as _re

    src = open("backend/system_control/weather_bridge_unified.py").read()
    tree = _ast.parse(src)
    ns = {"re": _re}
    for node in tree.body:
        keep = (
            isinstance(node, _ast.Assign)
            and any(getattr(t, "id", "").startswith("_WEATHER") for t in node.targets)
        ) or (
            isinstance(node, _ast.FunctionDef) and node.name == "_is_weather_query"
        )
        if keep:
            exec(compile(_ast.Module([node], []), "<x>", "exec"), ns)
    return ns["_is_weather_query"]


@pytest.mark.parametrize("phrase", [
    "close the window",                 # 'wind' — the one that kept firing
    "open a new window",
    "the model is warming up",          # 'warm'
    "training the brain",               # 'rain'
    "brainstorm the approach",
    "hotfix deployed",                  # 'hot'
    "scold the agent",                  # 'cold'
    "the cold start took 12 seconds",
])
def test_the_canonical_matcher_does_not_launch_the_app(phrase):
    assert _canonical()(phrase) is False, f"{phrase!r} would open the Weather app"


@pytest.mark.parametrize("phrase", [
    "what's the weather", "is it cold outside", "will it rain today",
    "check the temperature", "how hot is it outside", "what is the forecast",
])
def test_genuine_questions_still_route_through_the_bridge(phrase):
    assert _canonical()(phrase) is True


def test_the_bridge_no_longer_scans_substrings():
    """Structural pin. This predicate sits directly upstream of an
    `open -a Weather` subprocess call."""
    from pathlib import Path

    src = Path(
        "backend/system_control/weather_bridge_unified.py",
    ).read_text(encoding="utf-8")
    assert "for keyword in weather_keywords" not in src
    assert "_is_weather_query(text)" in src


def test_both_matchers_agree():
    """Two implementations that disagree are a bug waiting for whichever path
    the operator happens to take."""
    from backend.voice.intelligent_command_handler import _is_weather_query as a

    b = _canonical()
    for phrase in ("close the window", "training the brain", "hotfix deployed",
                   "what's the weather", "is it cold outside"):
        assert a(phrase) == b(phrase), f"matchers disagree on {phrase!r}"
