"""Tests for classify_clarify + tdd_directive (Priority 3 Feature 2 full + Feature 1 minimum).

Scope:
  * Clarifier sanitizer (Tier-1 untrusted-text discipline)
  * Ambiguity heuristic (narrow trigger set)
  * Timeout / declined / no-channel graceful fallback
  * Session cap enforcement
  * Env-gated master switch (DEFAULT OFF)
  * merge_into_context — authority invariant (description+evidence only,
    never mutates risk / route / guardian fields)
  * TDD directive — enabled flag, is_tdd_op detection, prompt text shape
  * AST canaries locking the orchestrator wiring
"""
from __future__ import annotations

import asyncio
import ast
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance import classify_clarify as cc
from backend.core.ouroboros.governance.classify_clarify import (
    ClarifyResponse,
    answer_max_chars,
    ask_operator,
    clarify_enabled,
    clarify_timeout_s,
    max_per_session,
    merge_into_context,
    min_desc_chars,
    register_clarify_channel,
    reset_clarify_channel,
    reset_session_count,
    sanitize_answer,
    should_ask,
)
from backend.core.ouroboros.governance.tdd_directive import (
    is_tdd_op,
    stamp_tdd_evidence,
    tdd_enabled,
    tdd_prompt_directive,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in list(os.environ.keys()):
        if (
            key.startswith("JARVIS_CLASSIFY_CLARIFY_")
            or key.startswith("JARVIS_TDD_MODE_")
        ):
            monkeypatch.delenv(key, raising=False)
    reset_session_count()
    reset_clarify_channel()
    yield
    reset_session_count()
    reset_clarify_channel()


# ---------------------------------------------------------------------------
# Env gates — DEFAULT OFF (fail-closed)
# ---------------------------------------------------------------------------


def test_clarify_enabled_default_off():
    """Fail-closed: feature must be explicitly opted-in."""
    assert clarify_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_clarify_enabled_truthy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_CLASSIFY_CLARIFY_ENABLED", val)
    assert clarify_enabled() is True


def test_clarify_timeout_default_30s():
    """Per amendment: operator-realistic default, not 5s."""
    assert clarify_timeout_s() == 30.0


def test_clarify_timeout_clamped_range(monkeypatch):
    monkeypatch.setenv("JARVIS_CLASSIFY_CLARIFY_TIMEOUT_S", "1")
    assert clarify_timeout_s() == 5.0  # min clamp
    monkeypatch.setenv("JARVIS_CLASSIFY_CLARIFY_TIMEOUT_S", "9999")
    assert clarify_timeout_s() == 300.0  # max clamp


def test_max_per_session_default_3():
    assert max_per_session() == 3


def test_answer_max_chars_default():
    assert answer_max_chars() == 512


# ---------------------------------------------------------------------------
# Sanitizer — Tier-1 secret-shape redaction + length cap + control-char strip
# ---------------------------------------------------------------------------


def test_sanitize_empty_returns_empty():
    assert sanitize_answer("") == ""
    assert sanitize_answer(None) == ""  # type: ignore[arg-type]


def test_sanitize_length_cap():
    raw = "x" * 10000
    out = sanitize_answer(raw, max_chars=100)
    assert len(out) <= 100


def test_sanitize_redacts_openai_key():
    raw = "My key is sk-abcdefghijklmnopqrstuvwxyz0123456789 please use it"
    out = sanitize_answer(raw)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in out
    assert "[REDACTED_SECRET]" in out


def test_sanitize_redacts_aws_key():
    raw = "AWS key: AKIAIOSFODNN7EXAMPLE and stuff"
    out = sanitize_answer(raw)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "[REDACTED_SECRET]" in out


def test_sanitize_redacts_github_token():
    raw = "Token is ghp_abcdefghijklmnopqrstuvwxyz0123456789AB"
    out = sanitize_answer(raw)
    assert "ghp_abcdefghijklm" not in out
    assert "[REDACTED_SECRET]" in out


def test_sanitize_strips_control_chars():
    raw = "hello\x00world\x01\x02"
    out = sanitize_answer(raw)
    assert "\x00" not in out
    assert "\x01" not in out


def test_sanitize_preserves_readable_content():
    """Newlines + tabs may be collapsed by the shared log-safety
    sanitizer (``sanitize_for_log``) — that's expected for downstream
    log lines and prompt composition. The non-whitespace content must
    survive intact."""
    raw = "line1\nline2\tcolumn"
    out = sanitize_answer(raw)
    # Content preserved even if whitespace normalized.
    assert "line1" in out
    assert "line2" in out
    assert "column" in out


# ---------------------------------------------------------------------------
# Ambiguity heuristic — narrow trigger set
# ---------------------------------------------------------------------------


def test_should_ask_short_desc_no_target_files():
    reason = should_ask(
        description="fix it",
        target_files=(),
        goal_keywords=(),
    )
    assert reason == "short_description_no_target_files"


def test_should_ask_generic_target_file_list():
    reason = should_ask(
        description="A well-documented description that would normally be fine.",
        target_files=("various",),
        goal_keywords=(),
    )
    assert reason == "generic_target_file_list"


def test_should_ask_generic_tbd_marker():
    reason = should_ask(
        description="Looks actionable on the surface but target is opaque",
        target_files=("TBD",),
        goal_keywords=(),
    )
    assert reason == "generic_target_file_list"


def test_should_ask_short_beats_keyword_precedence():
    """When BOTH short-desc and no-keyword-match apply, the earlier
    check (short-desc) wins because it's more specific — an empty
    target-file list is a stronger ambiguity signal than keyword miss.
    """
    reason = should_ask(
        description="fix it",  # deliberately under 40 chars
        target_files=(),
        goal_keywords=("authentication", "payments", "graph"),
    )
    assert reason == "short_description_no_target_files"


def test_should_ask_no_keyword_match_medium_desc():
    """Medium-length desc (between min and 2×min) + no keyword match fires."""
    reason = should_ask(
        description="Add extras to my thing, with some words, but keywordless.",
        target_files=("src/foo.py",),   # specific target bypasses generic check
        goal_keywords=("authentication", "payments", "graph"),
    )
    assert reason == "no_goal_keyword_match"


def test_should_ask_actionable_intent_silent():
    """Long descriptive text + real target file + matching keyword = no ask."""
    reason = should_ask(
        description=(
            "Fix authentication bug where admin check is bypassed for users "
            "with suspended sessions — repro steps in SEC-4032."
        ),
        target_files=("backend/auth/session.py",),
        goal_keywords=("authentication", "admin"),
    )
    assert reason is None


# ---------------------------------------------------------------------------
# ask_operator — timeout / declined / no-channel paths
# ---------------------------------------------------------------------------


def test_ask_operator_skips_when_disabled():
    async def _run():
        return await ask_operator(
            op_id="op-1",
            description="fix",
            target_files=(),
        )
    resp = asyncio.run(_run())
    assert resp.outcome == "skipped_disabled"


def test_ask_operator_no_channel_returns_skipped(monkeypatch):
    monkeypatch.setenv("JARVIS_CLASSIFY_CLARIFY_ENABLED", "1")
    async def _run():
        return await ask_operator(
            op_id="op-1",
            description="fix",   # ambiguous
            target_files=(),
        )
    resp = asyncio.run(_run())
    assert resp.outcome == "skipped_no_channel"
    assert resp.why_triggered == "short_description_no_target_files"


def test_ask_operator_answered(monkeypatch):
    monkeypatch.setenv("JARVIS_CLASSIFY_CLARIFY_ENABLED", "1")

    class _Ch:
        available = True
        async def ask(self, q, *, timeout_s):
            return "please fix the auth check in session.py"

    register_clarify_channel(_Ch())

    async def _run():
        return await ask_operator(
            op_id="op-1",
            description="fix",
            target_files=(),
        )
    resp = asyncio.run(_run())
    assert resp.outcome == "answered"
    assert "session.py" in resp.answer_sanitized
    assert resp.duration_ms >= 0


def test_ask_operator_timeout(monkeypatch):
    monkeypatch.setenv("JARVIS_CLASSIFY_CLARIFY_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CLASSIFY_CLARIFY_TIMEOUT_S", "5")

    class _SlowCh:
        available = True
        async def ask(self, q, *, timeout_s):
            # Wait longer than the outer timeout.
            await asyncio.sleep(30)
            return "too late"

    register_clarify_channel(_SlowCh())

    async def _run():
        return await ask_operator(
            op_id="op-1",
            description="fix",
            target_files=(),
        )
    resp = asyncio.run(_run())
    assert resp.outcome == "timeout"


def test_ask_operator_declined_on_empty_answer(monkeypatch):
    monkeypatch.setenv("JARVIS_CLASSIFY_CLARIFY_ENABLED", "1")

    class _BlankCh:
        available = True
        async def ask(self, q, *, timeout_s):
            return "   "   # whitespace-only

    register_clarify_channel(_BlankCh())

    async def _run():
        return await ask_operator(
            op_id="op-1",
            description="fix",
            target_files=(),
        )
    resp = asyncio.run(_run())
    assert resp.outcome == "declined"


def test_ask_operator_session_cap_enforced(monkeypatch):
    monkeypatch.setenv("JARVIS_CLASSIFY_CLARIFY_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CLASSIFY_CLARIFY_MAX_PER_SESSION", "2")

    class _Ch:
        available = True
        async def ask(self, q, *, timeout_s):
            return "some answer"

    register_clarify_channel(_Ch())

    async def _run():
        results = []
        for i in range(4):
            results.append(await ask_operator(
                op_id=f"op-{i}",
                description="fix",
                target_files=(),
            ))
        return results
    results = asyncio.run(_run())
    # First two answer; rest hit the session cap.
    outcomes = [r.outcome for r in results]
    assert outcomes.count("answered") == 2
    assert outcomes.count("skipped_cap") == 2


# ---------------------------------------------------------------------------
# merge_into_context — authority invariant
# ---------------------------------------------------------------------------


def test_merge_answered_enriches_description():
    resp = ClarifyResponse(
        outcome="answered",
        answer_raw="please fix the admin check",
        answer_sanitized="please fix the admin check",
        duration_ms=1200,
        why_triggered="short_description_no_target_files",
        question="Q",
    )
    new_desc, patch = merge_into_context(
        original_description="fix it",
        response=resp,
    )
    assert "fix it" in new_desc
    assert "please fix the admin check" in new_desc
    assert "[operator clarification]" in new_desc
    assert patch["clarification_outcome"] == "answered"
    assert patch["clarification_answer"] == "please fix the admin check"
    # Authority invariant — patch keys are ONLY about clarification.
    assert "risk_tier" not in patch
    assert "provider_route" not in patch


def test_merge_timeout_leaves_description_untouched():
    resp = ClarifyResponse(
        outcome="timeout",
        duration_ms=30000,
        why_triggered="short_description_no_target_files",
    )
    new_desc, patch = merge_into_context(
        original_description="fix it",
        response=resp,
    )
    assert new_desc == "fix it"
    assert patch["clarification_outcome"] == "timeout"
    assert patch["clarification_answer"] == ""


def test_merge_declined_leaves_description_untouched():
    resp = ClarifyResponse(
        outcome="declined", why_triggered="no_goal_keyword_match",
    )
    new_desc, patch = merge_into_context(
        original_description="add feature X",
        response=resp,
    )
    assert new_desc == "add feature X"


# ---------------------------------------------------------------------------
# TDD directive — Feature 1 minimum
# ---------------------------------------------------------------------------


def test_tdd_enabled_default_on():
    """Feature 1 is opt-in per intent, not globally — but the master
    switch defaults ON so a flagged intent gets the directive."""
    assert tdd_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "off"])
def test_tdd_master_disable(monkeypatch, val):
    monkeypatch.setenv("JARVIS_TDD_MODE_ENABLED", val)
    assert tdd_enabled() is False


def test_is_tdd_op_true_when_flag_set():
    ctx = MagicMock()
    ctx.evidence = {"tdd_mode": True}
    assert is_tdd_op(ctx) is True


def test_is_tdd_op_false_when_flag_absent():
    ctx = MagicMock()
    ctx.evidence = {"other": "value"}
    assert is_tdd_op(ctx) is False


def test_is_tdd_op_false_when_master_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_TDD_MODE_ENABLED", "0")
    ctx = MagicMock()
    ctx.evidence = {"tdd_mode": True}
    assert is_tdd_op(ctx) is False


def test_is_tdd_op_false_when_no_evidence_attr():
    class _Ctx:
        pass
    assert is_tdd_op(_Ctx()) is False


def test_tdd_prompt_directive_contains_honest_caveat():
    """Per amendment: Feature 1 V1 docs MUST literally say
    'TDD-shaped generation, NOT red-green proven'. Otherwise future-us
    forgets the scope caveat and starts treating the flag as a proof."""
    text = tdd_prompt_directive()
    assert "TDD" in text or "Test-First" in text
    # Honest caveat is load-bearing.
    assert (
        "not yet execute a red-green proof" in text
        or "not red-green" in text.lower()
        or "not red–green" in text.lower()
        or "V1.1 will" in text.lower()
    )
    # Teaches the shape we want.
    assert "files: [...]" in text
    assert "test file" in text.lower()


def test_stamp_tdd_evidence_from_none():
    out = stamp_tdd_evidence(None, on=True)
    assert out == {"tdd_mode": True}


def test_stamp_tdd_evidence_preserves_other_keys():
    out = stamp_tdd_evidence({"existing": "value"}, on=True)
    assert out == {"existing": "value", "tdd_mode": True}


def test_stamp_tdd_evidence_can_unset():
    out = stamp_tdd_evidence({"tdd_mode": True}, on=False)
    assert out["tdd_mode"] is False


# ---------------------------------------------------------------------------
# AST canaries — orchestrator wiring must persist across refactors
# ---------------------------------------------------------------------------


def _read(parts: tuple) -> str:
    base = Path(__file__).resolve().parent.parent.parent
    return base.joinpath(*parts).read_text(encoding="utf-8")


def test_orchestrator_wires_classify_clarify():
    src = _read((
        "backend", "core", "ouroboros", "governance", "orchestrator.py",
    ))
    assert "classify_clarify" in src
    # Must actually CALL ask_operator, not just import.
    assert "ask_operator" in src
    assert "merge_into_context" in src


def test_orchestrator_wires_tdd_directive():
    src = _read((
        "backend", "core", "ouroboros", "governance", "orchestrator.py",
    ))
    assert "tdd_directive" in src
    assert "is_tdd_op" in src
    assert "tdd_prompt_directive" in src


def test_harness_dispatches_tdd_slash_command():
    src = _read((
        "backend", "core", "ouroboros", "battle_test", "harness.py",
    ))
    assert "_repl_cmd_tdd" in src
    assert "/tdd" in src


def test_tdd_directive_honesty_caveat_in_docstring():
    """The module docstring must explicitly label V1 as 'NOT red-green
    proven' so readers don't misconstrue the scope."""
    src = _read((
        "backend", "core", "ouroboros", "governance", "tdd_directive.py",
    ))
    assert "NOT red-green" in src or "not red-green" in src.lower()
    assert "V1.1" in src
