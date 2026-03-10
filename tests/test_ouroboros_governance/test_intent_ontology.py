"""
Tests for Task 1: Unify Intent Ontology — CAI + IntelligentModelSelector

Covers:
1. CAI predicts all 6 new Ouroboros codegen intents correctly.
2. IntelligentModelSelector._classify_intent() returns correct intent strings
   (exercises the keyword-fallback path that is always available in CI).
3. _intent_to_required_and_preferred() returns correct capability sets for
   each codegen intent.
4. _estimate_complexity() returns correct tier for each codegen intent.
"""

from __future__ import annotations

from typing import Set, Tuple
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cai(tmp_path):
    """Construct a CAI instance backed by a temp SQLite DB to avoid side effects."""
    from backend.intelligence.context_awareness_intelligence import (
        ContextAwarenessIntelligence,
    )

    with patch(
        "backend.intelligence.context_awareness_intelligence.create_enhanced_cai",
        return_value=MagicMock(),
    ):
        return ContextAwarenessIntelligence(db_path=tmp_path / "cai_test.sqlite3")


def _make_selector():
    """Construct an IntelligentModelSelector with stubbed registry/lifecycle."""
    from backend.intelligence.model_selector import IntelligentModelSelector

    with (
        patch("backend.intelligence.model_selector.get_model_registry", return_value=MagicMock()),
        patch("backend.intelligence.model_selector.get_lifecycle_manager", return_value=MagicMock()),
    ):
        return IntelligentModelSelector()


# ---------------------------------------------------------------------------
# Test 1 — CAI predicts codegen intents
# ---------------------------------------------------------------------------

CODEGEN_INTENT_SAMPLES = [
    ("code_generation", "Implement a new UserAuthService class with JWT support"),
    ("bug_fix", "Fix the broken token refresh endpoint that is failing in production"),
    ("segfault_analysis", "There is a segfault in the memory allocator when null pointer is dereferenced"),
    ("heavy_refactor", "Refactor the monolithic database module by extracting a repository class"),
    ("architecture_design", "Design the cross-repo architecture for migrating the monolith to microservices"),
    ("single_line_change", "Append a single line comment at the end of the config file"),
]


@pytest.mark.parametrize("expected_intent,sample_text", CODEGEN_INTENT_SAMPLES)
def test_cai_predicts_codegen_intents(tmp_path, expected_intent, sample_text):
    """CAI.predict_intent() must return the expected codegen intent for representative inputs."""
    cai = _make_cai(tmp_path)
    result = cai.predict_intent(sample_text)

    assert result["intent"] == expected_intent, (
        f"Expected intent={expected_intent!r} for text={sample_text!r}; "
        f"got intent={result['intent']!r} (confidence={result['confidence']:.3f}). "
        f"Top alternatives: {result.get('alternatives', [])}"
    )
    assert result["confidence"] > 0.0, "Confidence should be positive"
    assert "suggestion" in result, "Result must include a suggestion"
    assert result["suggestion"], "Suggestion should be non-empty for codegen intents"


# ---------------------------------------------------------------------------
# Test 2 — IntelligentModelSelector._classify_intent() via fallback path
# ---------------------------------------------------------------------------

CLASSIFY_INTENT_CASES = [
    # (query, expected_intent)
    ("segfault in null pointer dereference", "segfault_analysis"),
    ("cross-repo architecture design", "architecture_design"),
    ("refactor the legacy module", "heavy_refactor"),
    ("fix the broken login bug", "bug_fix"),
    ("implement a new cache layer", "code_generation"),
    ("append a comment at the end of the file", "single_line_change"),
    # Existing voice/chat intents remain intact
    ("show me what's on the screen", "vision_analysis"),
    ("chat with me about Python", "conversational_ai"),
    ("search for my last commit message", "semantic_search"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("query,expected_intent", CLASSIFY_INTENT_CASES)
async def test_intelligent_model_selector_classify_intent(query, expected_intent):
    """_classify_intent() must return the correct intent via keyword fallback."""
    selector = _make_selector()

    # Force CAI to be unavailable so the keyword fallback path is exercised
    selector._cai = None
    result = await selector._classify_intent(query)

    assert result == expected_intent, (
        f"Query={query!r}: expected intent={expected_intent!r}, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — _intent_to_required_and_preferred() for codegen intents
# ---------------------------------------------------------------------------

CAPABILITY_CASES: list[tuple[str, Set[str], Set[str]]] = [
    ("code_generation",    {"code_generation"},                   {"response_generation"}),
    ("bug_fix",            {"code_generation"},                   {"response_generation"}),
    ("segfault_analysis",  {"code_generation"},                   {"complex_reasoning"}),
    ("heavy_refactor",     {"code_generation", "code_refactor"},  {"code_generation"}),
    ("architecture_design",{"complex_reasoning"},                 {"code_generation", "response_generation"}),
    ("single_line_change", {"chat"},                              {"trivial_ops", "code_generation"}),
]


@pytest.mark.parametrize("intent,expected_required,expected_preferred", CAPABILITY_CASES)
def test_intent_to_capabilities_codegen(intent, expected_required, expected_preferred):
    """_intent_to_required_and_preferred() must return correct capability sets."""
    selector = _make_selector()
    required, preferred = selector._intent_to_required_and_preferred(intent)

    assert required == expected_required, (
        f"Intent={intent!r}: required={required!r}, expected={expected_required!r}"
    )
    assert preferred == expected_preferred, (
        f"Intent={intent!r}: preferred={preferred!r}, expected={expected_preferred!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — _estimate_complexity() for codegen intents
# ---------------------------------------------------------------------------

COMPLEXITY_CASES = [
    ("segfault_analysis",   "some query", "complex"),
    ("architecture_design", "some query", "complex"),
    ("heavy_refactor",      "some query", "complex"),
    ("bug_fix",             "some query", "medium"),
    ("code_generation",     "some query", "medium"),
    ("single_line_change",  "some query", "simple"),
]


@pytest.mark.parametrize("intent,query,expected_complexity", COMPLEXITY_CASES)
def test_estimate_complexity_codegen(intent, query, expected_complexity):
    """_estimate_complexity() must return the correct tier for each codegen intent."""
    selector = _make_selector()
    result = selector._estimate_complexity(query, intent)

    assert result == expected_complexity, (
        f"Intent={intent!r}: expected complexity={expected_complexity!r}, got {result!r}"
    )
