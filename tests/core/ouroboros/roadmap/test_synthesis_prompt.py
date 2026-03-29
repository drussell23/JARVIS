"""
Tests for synthesis_prompt.py
===============================

Coverage:
- test_build_prompt_includes_fragments
- test_build_prompt_includes_tier0_hints
- test_build_prompt_includes_json_schema
- test_shed_context_under_budget
- test_shed_context_over_budget_raises
- test_shed_context_truncates
- test_json_schema_has_required_fields
"""

from __future__ import annotations

import hashlib
import time
from typing import List

import pytest

from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot, SnapshotFragment
from backend.core.ouroboros.roadmap.synthesis_prompt import (
    SYNTHESIS_JSON_SCHEMA,
    ContextBudgetExceededError,
    _CHARS_PER_TOKEN,
    build_synthesis_prompt,
    shed_context,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fragment(
    source_id: str = "spec:whatsapp-integration",
    summary: str = "WhatsApp integration design spec",
    tier: int = 0,
) -> SnapshotFragment:
    return SnapshotFragment(
        source_id=source_id,
        uri="docs/whatsapp.md",
        tier=tier,
        content_hash=hashlib.sha256(summary.encode()).hexdigest(),
        fetched_at=time.time(),
        mtime=time.time(),
        title="WhatsApp Integration Spec",
        summary=summary,
        fragment_type="spec",
    )


def _make_snapshot(fragments: tuple = ()) -> RoadmapSnapshot:
    if not fragments:
        fragments = (_make_fragment(),)
    return RoadmapSnapshot.create(fragments=fragments)


def _make_hint(
    description: str = "Missing WhatsApp capability",
    snapshot_hash: str = "abc123",
) -> FeatureHypothesis:
    return FeatureHypothesis.new(
        description=description,
        evidence_fragments=("spec:whatsapp-integration",),
        gap_type="missing_capability",
        confidence=0.9,
        confidence_rule_id="spec_symbol_miss",
        urgency="high",
        suggested_scope="new-agent",
        suggested_repos=("jarvis",),
        provenance="deterministic",
        synthesized_for_snapshot_hash=snapshot_hash,
        synthesis_input_fingerprint="fp_abc123",
    )


# ---------------------------------------------------------------------------
# build_synthesis_prompt
# ---------------------------------------------------------------------------

def test_build_prompt_includes_fragments():
    """P0 fragment summary content should appear in the generated prompt."""
    snapshot = _make_snapshot()
    prompt = build_synthesis_prompt(snapshot, [], "oracle summary")
    # The fragment summary contains "WhatsApp" — it must appear in the prompt.
    assert "WhatsApp" in prompt


def test_build_prompt_includes_tier0_hints():
    """Tier 0 hint descriptions should appear in the generated prompt."""
    snapshot = _make_snapshot()
    hint = _make_hint(description="Missing WhatsApp integration layer")
    prompt = build_synthesis_prompt(snapshot, [hint], "oracle summary")
    assert "Missing WhatsApp integration layer" in prompt


def test_build_prompt_includes_json_schema():
    """Key schema field names must appear in the generated prompt."""
    snapshot = _make_snapshot()
    prompt = build_synthesis_prompt(snapshot, [], "oracle summary")
    assert "description" in prompt
    assert "gap_type" in prompt


def test_build_prompt_oracle_summary_included():
    """Oracle summary text should appear verbatim in the prompt."""
    snapshot = _make_snapshot()
    oracle_text = "TheOracle detected 7 nodes related to messaging"
    prompt = build_synthesis_prompt(snapshot, [], oracle_text)
    assert oracle_text in prompt


def test_build_prompt_only_p0_fragments_in_body():
    """Non-P0 (tier > 0) fragments must NOT appear in the prompt fragment block."""
    p0_frag = _make_fragment(source_id="spec:p0", summary="P0 authoritative spec", tier=0)
    p1_frag = _make_fragment(source_id="plan:p1", summary="P1 trajectory plan", tier=1)
    snapshot = _make_snapshot(fragments=(p0_frag, p1_frag))
    prompt = build_synthesis_prompt(snapshot, [], "oracle summary")
    assert "P0 authoritative spec" in prompt
    assert "P1 trajectory plan" not in prompt


def test_build_prompt_returns_string():
    """build_synthesis_prompt always returns a str."""
    snapshot = _make_snapshot()
    result = build_synthesis_prompt(snapshot, [], "")
    assert isinstance(result, str)
    assert len(result) > 0


def test_build_prompt_multiple_hints():
    """Multiple hints should all appear in the prompt."""
    snapshot = _make_snapshot()
    hints = [
        _make_hint(description="Gap alpha"),
        _make_hint(description="Gap beta"),
    ]
    prompt = build_synthesis_prompt(snapshot, hints, "oracle summary")
    assert "Gap alpha" in prompt
    assert "Gap beta" in prompt


def test_build_prompt_empty_fragments_graceful():
    """A snapshot with no P0 fragments should not crash."""
    p1_frag = _make_fragment(source_id="plan:only", summary="only a plan", tier=1)
    snapshot = _make_snapshot(fragments=(p1_frag,))
    prompt = build_synthesis_prompt(snapshot, [], "oracle summary")
    assert isinstance(prompt, str)
    assert "no P0 fragments" in prompt


# ---------------------------------------------------------------------------
# shed_context
# ---------------------------------------------------------------------------

def test_shed_context_under_budget():
    """Text that fits within the budget is returned unchanged."""
    text = "short text"
    result = shed_context(text, max_tokens=1000)
    assert result == text


def test_shed_context_over_budget_raises():
    """A budget of 0 (or negative) raises ContextBudgetExceededError."""
    huge_text = "x" * 10_000
    with pytest.raises(ContextBudgetExceededError):
        shed_context(huge_text, max_tokens=0)


def test_shed_context_truncates():
    """Text longer than max_tokens * _CHARS_PER_TOKEN is truncated."""
    # Make text that requires 1000 tokens but we only allow 100.
    text = "a" * (1000 * _CHARS_PER_TOKEN)
    result = shed_context(text, max_tokens=100)
    assert len(result) < len(text)
    assert "[truncated]" in result


def test_shed_context_exact_budget():
    """Text exactly at the token limit is returned unchanged."""
    text = "b" * (50 * _CHARS_PER_TOKEN)
    result = shed_context(text, max_tokens=50)
    assert result == text


def test_shed_context_negative_budget_raises():
    """Negative max_tokens raises ContextBudgetExceededError."""
    with pytest.raises(ContextBudgetExceededError):
        shed_context("anything", max_tokens=-5)


def test_shed_context_preserves_prefix():
    """The beginning of the text is preserved after truncation."""
    text = "IMPORTANT_PREFIX " + ("filler " * 2000)
    result = shed_context(text, max_tokens=10)
    assert result.startswith("IMPORTANT_PREFIX")


def test_shed_context_empty_string():
    """Empty string is always within budget."""
    result = shed_context("", max_tokens=1)
    assert result == ""


# ---------------------------------------------------------------------------
# SYNTHESIS_JSON_SCHEMA
# ---------------------------------------------------------------------------

def test_json_schema_has_required_fields():
    """SYNTHESIS_JSON_SCHEMA must declare all mandatory gap item fields."""
    required_fields = {
        "description",
        "evidence_fragments",
        "gap_type",
        "confidence",
        "urgency",
        "suggested_scope",
        "suggested_repos",
    }
    items_required = set(
        SYNTHESIS_JSON_SCHEMA["properties"]["gaps"]["items"]["required"]
    )
    assert required_fields.issubset(items_required), (
        f"Missing required fields: {required_fields - items_required}"
    )


def test_json_schema_gap_type_enum():
    """gap_type must enumerate all four valid types."""
    valid_types = {
        "missing_capability",
        "incomplete_wiring",
        "stale_implementation",
        "manifesto_violation",
    }
    enum_values = set(
        SYNTHESIS_JSON_SCHEMA["properties"]["gaps"]["items"]["properties"]["gap_type"]["enum"]
    )
    assert enum_values == valid_types


def test_json_schema_is_dict():
    """SYNTHESIS_JSON_SCHEMA is a plain dict (JSON-serialisable)."""
    assert isinstance(SYNTHESIS_JSON_SCHEMA, dict)
    # Should be round-trippable via json.dumps
    import json
    serialised = json.dumps(SYNTHESIS_JSON_SCHEMA)
    assert isinstance(serialised, str)
    assert len(serialised) > 0
