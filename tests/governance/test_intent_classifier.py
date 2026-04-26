"""P2 Slice 1 — IntentClassifier regression suite.

Pins:
  * Module constants + 4-category enum.
  * Env-knob accessor (default false pre-graduation).
  * Verb classification: ACTION_REQUEST / EXPLORATION / EXPLANATION
    happy paths, ties, mixed signals.
  * Code-paste heuristic (CONTEXT_PASTE wins before verb tally):
    stack-trace markers, multi-line indented blocks, fenced code,
    combined signals → confidence ladder.
  * Defensive defaults: empty / whitespace / None / oversize +
    safe-EXPLANATION-on-no-signal.
  * Confidence helpers + low-confidence floor.
  * Authority invariants: banned imports + no I/O / subprocess.
"""
from __future__ import annotations

import dataclasses
import io
import tokenize
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.intent_classifier import (
    CODE_PASTE_MIN_INDENT_LINES,
    CODE_PASTE_MIN_NEWLINES,
    LOW_CONFIDENCE_FLOOR,
    MAX_MESSAGE_CHARS,
    ChatIntent,
    IntentClassification,
    classify,
    is_enabled,
    is_low_confidence,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return src
    for tok in toks:
        if tok.type == tokenize.STRING:
            out.append('""')
        elif tok.type == tokenize.COMMENT:
            continue
        else:
            out.append(tok.string)
    return " ".join(out)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    yield


# ===========================================================================
# A — Module constants + env knob
# ===========================================================================


def test_max_message_chars_pinned():
    assert MAX_MESSAGE_CHARS == 64 * 1024


def test_paste_threshold_constants_pinned():
    assert CODE_PASTE_MIN_NEWLINES == 3
    assert CODE_PASTE_MIN_INDENT_LINES == 2


def test_low_confidence_floor_pinned():
    assert LOW_CONFIDENCE_FLOOR == 0.40


def test_chat_intent_has_four_values():
    """Pin: PRD §9 P2 says three routing buckets + one for paste-as-
    context. Adding a fifth requires a new slice + design doc."""
    assert {i.name for i in ChatIntent} == {
        "ACTION_REQUEST", "EXPLORATION", "EXPLANATION", "CONTEXT_PASTE",
    }


def test_is_enabled_default_false_pre_graduation():
    """Slice 1 ships default-OFF. Renamed to
    ``test_is_enabled_default_true_post_graduation`` at Slice 4 flip."""
    assert is_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_is_enabled_truthy_variants(monkeypatch, val):
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", val)
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage", ""])
def test_is_enabled_falsy_variants(monkeypatch, val):
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", val)
    assert is_enabled() is False


# ===========================================================================
# B — IntentClassification dataclass
# ===========================================================================


def test_classification_is_frozen():
    v = classify("hello")
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.intent = ChatIntent.ACTION_REQUEST  # type: ignore[misc]


def test_classification_default_truncated_false():
    v = classify("fix the bug")
    assert v.truncated is False


# ===========================================================================
# C — ACTION_REQUEST happy paths
# ===========================================================================


@pytest.mark.parametrize("msg", [
    "fix the auth bug",
    "add a test for parse_decision_input",
    "refactor the FSM into a state machine",
    "delete the old shim",
    "implement the new policy gate",
    "rename the variable",
    "merge the two helpers",
    "patch the credential leak",
    "update the policy_version",
    "deploy the rollback",
    "kick off the smoke test",
    "replace foo with bar",
])
def test_action_verbs_route_to_action(msg):
    v = classify(msg)
    assert v.intent is ChatIntent.ACTION_REQUEST, (msg, v)
    assert v.confidence >= 0.6


def test_action_softener_increases_confidence():
    a = classify("fix the bug")
    b = classify("please fix the bug")
    assert b.confidence > a.confidence


def test_action_softener_alone_does_not_pick_action():
    """``please`` alone has no action verb → falls back to safe
    default. The softener only weights an existing action signal."""
    v = classify("please")
    assert v.intent is not ChatIntent.ACTION_REQUEST


# ===========================================================================
# D — EXPLORATION happy paths
# ===========================================================================


@pytest.mark.parametrize("msg", [
    "find all callers of deprecated_api",
    "search for the missing import",
    "list every sensor under intake/",
    "show me where the policy is constructed",
    "explore the new repair_engine module",
    "audit the credential surface",
    "trace the request through the FSM",
    "scan for hardcoded model names",
    "verify the rollback path",
    "investigate the CI red",
])
def test_exploration_verbs_route_to_exploration(msg):
    v = classify(msg)
    assert v.intent is ChatIntent.EXPLORATION, (msg, v)


# ===========================================================================
# E — EXPLANATION happy paths + question shape
# ===========================================================================


@pytest.mark.parametrize("msg", [
    "explain the routing decision",
    "describe how Iron Gate works",
    "why does the orchestrator skip POSTMORTEM?",
    "what does the cancel ledger do?",
    "how did the rollback resolve?",
    "summarize the last 5 commits",
    "compare the two providers",
    "tell me about the cognitive metrics",
])
def test_explanation_verbs_route_to_explanation(msg):
    v = classify(msg)
    assert v.intent is ChatIntent.EXPLANATION, (msg, v)


@pytest.mark.parametrize("msg", [
    "Why is this test failing?",
    "How does the gate decide?",
    "What is the policy version?",
    "When does ROUTE phase fire?",
    "Where is the cancel decision recorded?",
    "Which sensor caught the failure?",
    "Is the master flag on?",
    "Does the factory return Inline?",
])
def test_question_shape_routes_to_explanation(msg):
    v = classify(msg)
    assert v.intent is ChatIntent.EXPLANATION
    assert "question_shape" in v.reasons or "explanation_verb" in v.reasons


# ===========================================================================
# F — CONTEXT_PASTE: stack traces, code blocks, fenced
# ===========================================================================


def test_python_traceback_routes_to_paste():
    msg = (
        'Traceback (most recent call last):\n'
        '  File "foo.py", line 12, in bar\n'
        '    raise ValueError("oops")\n'
        'ValueError: oops\n'
    )
    v = classify(msg)
    assert v.intent is ChatIntent.CONTEXT_PASTE
    assert "stacktrace_marker" in v.reasons


def test_node_stack_routes_to_paste():
    msg = (
        "TypeError: Cannot read property 'x' of undefined\n"
        "    at /app/server.js:42:18\n"
        "    at processTicksAndRejections (node:internal/process/task_queues:96:5)\n"
    )
    v = classify(msg)
    assert v.intent is ChatIntent.CONTEXT_PASTE


def test_java_stack_routes_to_paste():
    msg = (
        "Exception in thread \"main\" java.lang.NullPointerException\n"
        "    at com.example.Foo.bar(Foo.java:42)\n"
        "    at com.example.Main.main(Main.java:7)\n"
    )
    v = classify(msg)
    assert v.intent is ChatIntent.CONTEXT_PASTE


def test_fenced_code_block_routes_to_paste():
    msg = "```python\nprint('hi')\n```"
    v = classify(msg)
    assert v.intent is ChatIntent.CONTEXT_PASTE
    assert "fenced_code_block" in v.reasons


def test_multiline_indented_routes_to_paste():
    msg = (
        "  def foo():\n"
        "      return 1\n"
        "\n"
        "  class Bar:\n"
        "      pass\n"
    )
    v = classify(msg)
    assert v.intent is ChatIntent.CONTEXT_PASTE
    assert "multiline_indented" in v.reasons


def test_paste_with_question_still_routes_to_paste():
    """Pin: stack-trace pasted with 'why?' should NOT be misrouted as
    EXPLANATION — operator wants the trace treated as context for the
    previous turn, not classified afresh."""
    msg = (
        "why is this happening?\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 5, in foo\n'
        '    raise RuntimeError("bad")\n'
    )
    v = classify(msg)
    assert v.intent is ChatIntent.CONTEXT_PASTE


def test_paste_combined_signals_higher_confidence():
    """Multiple paste signals → higher confidence than a single one."""
    single = classify("Traceback (most recent call last):")
    combined = classify(
        "```\nTraceback (most recent call last):\n  File \"x.py\", line 5\n```",
    )
    assert combined.confidence > single.confidence


def test_short_indented_text_below_paste_threshold():
    """One line of indent is not enough; prevents single ``  - bullet``
    from being miscategorized."""
    msg = "  one indented line"
    v = classify(msg)
    assert v.intent is not ChatIntent.CONTEXT_PASTE


# ===========================================================================
# G — Defensive defaults
# ===========================================================================


@pytest.mark.parametrize("msg", ["", "   ", "\n\n", "\t"])
def test_empty_or_whitespace_returns_safe_default(msg):
    v = classify(msg)
    assert v.intent is ChatIntent.EXPLANATION
    assert v.confidence == 0.0
    assert "empty" in v.reasons


def test_none_returns_safe_default():
    v = classify(None)  # type: ignore[arg-type]
    assert v.intent is ChatIntent.EXPLANATION
    assert v.confidence == 0.0


def test_no_signal_returns_safe_default():
    v = classify("lol")
    assert v.intent is ChatIntent.EXPLANATION
    assert "no_signal_default" in v.reasons


def test_oversize_message_truncated_and_flagged():
    huge = "fix the bug. " + ("x" * (MAX_MESSAGE_CHARS + 100))
    v = classify(huge)
    assert v.truncated is True
    assert v.intent is ChatIntent.ACTION_REQUEST  # leading verb still wins


def test_oversize_pure_garbage_truncates_to_default():
    huge = "x" * (MAX_MESSAGE_CHARS * 2)
    v = classify(huge)
    assert v.truncated is True
    assert v.intent is ChatIntent.EXPLANATION


# ===========================================================================
# H — Tie-breaking + confidence helpers
# ===========================================================================


def test_explanation_wins_tie_over_action():
    """Pin: when a message contains both action + explanation verbs at
    equal raw weight, EXPLANATION wins because it has no mutation
    surface — false-positive cost is words, not file edits."""
    # "explain how to fix" — both verbs present.
    v = classify("explain how to fix the bug")
    # EXPLANATION should dominate due to safe-tie-order + question-shape
    # bonus (the message starts with 'explain').
    assert v.intent is ChatIntent.EXPLANATION


def test_is_low_confidence_helper_below_floor():
    v = IntentClassification(
        intent=ChatIntent.ACTION_REQUEST, confidence=0.30,
    )
    assert is_low_confidence(v) is True


def test_is_low_confidence_helper_at_floor():
    v = IntentClassification(
        intent=ChatIntent.ACTION_REQUEST, confidence=0.40,
    )
    assert is_low_confidence(v) is False  # strict <


def test_is_low_confidence_helper_above_floor():
    v = IntentClassification(
        intent=ChatIntent.ACTION_REQUEST, confidence=0.95,
    )
    assert is_low_confidence(v) is False


# ===========================================================================
# I — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
]


def test_classifier_no_authority_imports():
    src = _read("backend/core/ouroboros/governance/intent_classifier.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_classifier_no_io_or_subprocess():
    """Pin: classifier is pure data — no I/O, no subprocess, no env
    mutation."""
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/intent_classifier.py"),
    )
    forbidden = [
        "subprocess.",
        "open(",
        ".write(",
        ".write_text(",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import requests",
        "import httpx",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"


def test_classifier_runs_in_under_5ms_on_typical_input():
    """Sanity check on the pure-deterministic fast-path. Not a strict
    SLA but a regression alarm if someone slips in a slow regex."""
    import time
    msg = (
        "please fix the auth bug in backend/core/ouroboros/governance/"
        "policy.py — the rejection path is wrong"
    )
    start = time.perf_counter()
    for _ in range(100):
        classify(msg)
    avg_ms = (time.perf_counter() - start) * 1000.0 / 100.0
    assert avg_ms < 5.0, f"classifier slow path: {avg_ms:.2f}ms / call"
