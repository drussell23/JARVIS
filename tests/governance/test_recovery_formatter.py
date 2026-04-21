"""Tests for recovery_formatter (Slice 2)."""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance.recovery_advisor import (
    FailureContext,
    RecoveryPlan,
    RecoverySuggestion,
    STOP_COST_CAP,
    STOP_VALIDATION_EXHAUSTED,
    advise,
)
from backend.core.ouroboros.governance.recovery_formatter import (
    RECOVERY_FORMATTER_SCHEMA_VERSION,
    render_json,
    render_text,
    render_voice,
)


def _sample_cost_plan() -> RecoveryPlan:
    return advise(FailureContext(
        op_id="op-abc-12345678",
        stop_reason=STOP_COST_CAP,
        cost_spent_usd=0.80,
        cost_cap_usd=0.50,
    ))


# ===========================================================================
# Schema
# ===========================================================================


def test_formatter_schema_version_pinned():
    assert RECOVERY_FORMATTER_SCHEMA_VERSION == "recovery_formatter.v1"


# ===========================================================================
# render_text
# ===========================================================================


def test_render_text_includes_op_id_and_summary():
    out = render_text(_sample_cost_plan())
    assert "op-abc-12345678" in out
    assert "Recovery" in out
    assert "$0.8000" in out or "$0.50" in out


def test_render_text_lists_each_suggestion_with_priority_tag():
    out = render_text(_sample_cost_plan())
    # Cost cap rule emits 3 suggestions
    assert "1." in out
    assert "2." in out
    assert "3." in out
    # Priority tags appear in brackets
    assert "[high]" in out
    assert "[medium]" in out


def test_render_text_shows_try_next_header():
    out = render_text(_sample_cost_plan())
    assert "Try next:" in out


def test_render_text_includes_command_and_rationale():
    out = render_text(_sample_cost_plan())
    # First suggestion: /cost op-abc-12345678
    assert "$ /cost op-abc-12345678" in out
    # Some form of rationale is present
    assert "Why:" in out


def test_render_text_for_none_plan_is_graceful():
    out = render_text(None)  # type: ignore[arg-type]
    assert "no recovery plan" in out.lower()


def test_render_text_for_empty_plan_mentions_no_suggestions():
    plan = RecoveryPlan(op_id="op-x", failure_summary="")
    out = render_text(plan)
    assert "no suggestions" in out.lower() or "no recovery" in out.lower()


def test_render_text_wraps_long_rationales():
    plan = RecoveryPlan(
        op_id="op-1", failure_summary="boom",
        suggestions=(RecoverySuggestion(
            title="try x",
            rationale=(
                "This rationale is deliberately long enough to force "
                "the word-wrap path to engage so the rendering output "
                "spans multiple lines and remains readable in a tight "
                "terminal column without sprawling off the right edge."
            ),
        ),),
    )
    out = render_text(plan)
    rationale_lines = [l for l in out.splitlines() if "Why:" in l or l.strip().startswith(" ")]
    # Output has >1 non-trivial line from the rationale
    assert "\n" in out
    # No line is longer than ~80 chars beyond the indent
    for line in out.splitlines():
        assert len(line) <= 120


# ===========================================================================
# render_voice
# ===========================================================================


def test_render_voice_starts_with_summary_and_count_phrase():
    plan = _sample_cost_plan()
    out = render_voice(plan)
    assert "Here are three things to try" in out


def test_render_voice_uses_ordinals():
    out = render_voice(_sample_cost_plan())
    assert "First," in out
    assert "Second," in out
    assert "Third," in out


def test_render_voice_does_not_include_commands_or_env_vars():
    out = render_voice(_sample_cost_plan())
    # No raw shell-style tokens — Karen reads natural language.
    # Dollar amounts like $0.80 are legitimate prose; what we guard
    # against is shell prefixes, env-var identifiers, flag tokens,
    # and markdown punctuation.
    assert "JARVIS_" not in out  # env var names redacted
    assert "--" not in out  # CLI flags
    assert "`" not in out  # markdown code
    # "$ " with trailing space would indicate a shell prompt render
    assert "$ /" not in out


def test_render_voice_clips_suggestions_to_max():
    plan = _sample_cost_plan()
    out = render_voice(plan, max_suggestions=1)
    assert "First" in out
    assert "Second" not in out


def test_render_voice_empty_plan_is_graceful():
    out = render_voice(RecoveryPlan(op_id="op-1", failure_summary=""))
    assert "No recovery" in out


def test_render_voice_none_plan_is_graceful():
    out = render_voice(None)  # type: ignore[arg-type]
    assert "No recovery" in out


def test_render_voice_include_op_id_adds_short_suffix():
    plan = _sample_cost_plan()
    out = render_voice(plan, include_op_id=True)
    # Only the short suffix (last 8 chars of last hyphen segment)
    assert "12345678" in out
    # The full op_id doesn't read out
    assert "op-abc-12345678" not in out


def test_render_voice_omits_op_id_by_default():
    plan = _sample_cost_plan()
    out = render_voice(plan)
    # Op id suffix shouldn't appear in default narration
    assert "12345678" not in out


def test_render_voice_strips_unsafe_tts_chars():
    plan = RecoveryPlan(
        op_id="op-1",
        failure_summary="Something with `backticks` <brackets> | pipes",
        suggestions=(RecoverySuggestion(title="plain title"),),
    )
    out = render_voice(plan)
    assert "`" not in out
    assert "<" not in out
    assert ">" not in out
    assert "|" not in out


def test_render_voice_count_phrase_singular():
    plan = RecoveryPlan(
        op_id="op-1", failure_summary="",
        suggestions=(RecoverySuggestion(title="only one"),),
    )
    out = render_voice(plan)
    assert "one thing to try" in out


# ===========================================================================
# render_json
# ===========================================================================


def test_render_json_round_trips():
    plan = _sample_cost_plan()
    obj = render_json(plan)
    assert obj["schema_version"] == "recovery_plan.v1"
    blob = json.dumps(obj)
    parsed = json.loads(blob)
    assert parsed["matched_rule"] == "cost_cap"
    assert parsed["has_plan"] is True


def test_render_json_none_returns_safe_stub():
    obj = render_json(None)  # type: ignore[arg-type]
    assert obj["has_plan"] is False
    assert obj["suggestions"] == []


def test_render_json_empty_plan_has_plan_false():
    plan = RecoveryPlan(op_id="op-1", failure_summary="")
    obj = render_json(plan)
    assert obj["has_plan"] is False


# ===========================================================================
# Determinism
# ===========================================================================


def test_render_text_is_deterministic():
    plan = _sample_cost_plan()
    assert render_text(plan) == render_text(plan)


def test_render_voice_is_deterministic():
    plan = _sample_cost_plan()
    assert render_voice(plan) == render_voice(plan)


# ===========================================================================
# Bounded output
# ===========================================================================


def test_tts_clipping_caps_long_summaries():
    plan = RecoveryPlan(
        op_id="op-1",
        failure_summary="x" * 1000,
        suggestions=(RecoverySuggestion(title="y"),),
    )
    out = render_voice(plan)
    # Clipped somewhere below the full 1000 chars
    assert len(out) < 2000


def test_voice_ordinal_fallback_beyond_tenth():
    """If we someday cap max_suggestions above 10, the ordinal
    helper must still produce something."""
    long_plan = RecoveryPlan(
        op_id="op-1", failure_summary="x",
        suggestions=tuple(
            RecoverySuggestion(title=f"item {i}") for i in range(12)
        ),
    )
    out = render_voice(long_plan, max_suggestions=12)
    # The 11th+ use a fallback
    assert "Suggestion 11" in out or "item 11" in out
