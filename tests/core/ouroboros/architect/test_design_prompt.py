"""
Tests for design_prompt module
================================

Covers:
- build_design_prompt includes hypothesis description in output
- build_design_prompt includes max_steps constraint in output
- build_design_prompt includes required JSON schema fields in output
- ARCHITECTURAL_PLAN_JSON_SCHEMA contains required top-level fields
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.architect.design_prompt import (
    ARCHITECTURAL_PLAN_JSON_SCHEMA,
    build_design_prompt,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_hypothesis(description: str = "Add WhatsApp integration to JARVIS messaging") -> MagicMock:
    """Return a duck-typed hypothesis mock with all required attributes."""
    h = MagicMock()
    h.description = description
    h.evidence_fragments = ("src-001", "src-002")
    h.gap_type = "missing_capability"
    h.suggested_scope = "new-agent"
    return h


# ---------------------------------------------------------------------------
# test_prompt_includes_hypothesis
# ---------------------------------------------------------------------------


def test_prompt_includes_hypothesis():
    """The hypothesis description is embedded verbatim in the prompt."""
    hypothesis = _make_hypothesis("Add WhatsApp integration to JARVIS messaging")
    prompt = build_design_prompt(hypothesis, oracle_context="some oracle context")
    assert "WhatsApp" in prompt


def test_prompt_includes_hypothesis_gap_type():
    """The hypothesis gap_type is embedded in the prompt."""
    hypothesis = _make_hypothesis()
    prompt = build_design_prompt(hypothesis, oracle_context="context")
    assert hypothesis.gap_type in prompt


def test_prompt_includes_hypothesis_suggested_scope():
    """The hypothesis suggested_scope is embedded in the prompt."""
    hypothesis = _make_hypothesis()
    prompt = build_design_prompt(hypothesis, oracle_context="context")
    assert hypothesis.suggested_scope in prompt


def test_prompt_includes_hypothesis_evidence_fragments():
    """At least one evidence_fragment source_id appears in the prompt."""
    hypothesis = _make_hypothesis()
    prompt = build_design_prompt(hypothesis, oracle_context="context")
    assert "src-001" in prompt or "src-002" in prompt


# ---------------------------------------------------------------------------
# test_prompt_includes_constraints
# ---------------------------------------------------------------------------


def test_prompt_includes_constraints():
    """The max_steps constraint value is present in the prompt."""
    hypothesis = _make_hypothesis()
    prompt = build_design_prompt(hypothesis, oracle_context="ctx", max_steps=7)
    assert "7" in prompt


def test_prompt_includes_default_max_steps():
    """Default max_steps=10 is present in the prompt when not overridden."""
    hypothesis = _make_hypothesis()
    prompt = build_design_prompt(hypothesis, oracle_context="ctx")
    assert "10" in prompt


def test_prompt_includes_file_rules():
    """Prompt instructs the model about file path rules (no hardcoding)."""
    hypothesis = _make_hypothesis()
    prompt = build_design_prompt(hypothesis, oracle_context="ctx")
    # Should mention path or file rules
    prompt_lower = prompt.lower()
    assert "path" in prompt_lower or "file" in prompt_lower


def test_prompt_includes_test_requirements():
    """Prompt instructs the model that each step requires tests."""
    hypothesis = _make_hypothesis()
    prompt = build_design_prompt(hypothesis, oracle_context="ctx")
    prompt_lower = prompt.lower()
    assert "test" in prompt_lower


def test_prompt_includes_dag_rules():
    """Prompt mentions DAG or dependency ordering rules."""
    hypothesis = _make_hypothesis()
    prompt = build_design_prompt(hypothesis, oracle_context="ctx")
    prompt_lower = prompt.lower()
    assert "depend" in prompt_lower or "dag" in prompt_lower or "order" in prompt_lower


def test_prompt_includes_non_goals_instruction():
    """Prompt instructs the model to declare non-goals."""
    hypothesis = _make_hypothesis()
    prompt = build_design_prompt(hypothesis, oracle_context="ctx")
    assert "non_goal" in prompt or "non-goal" in prompt.lower()


# ---------------------------------------------------------------------------
# test_prompt_includes_json_schema_fields
# ---------------------------------------------------------------------------


def test_prompt_includes_json_schema_fields():
    """Core schema field names are embedded in the prompt."""
    hypothesis = _make_hypothesis()
    prompt = build_design_prompt(hypothesis, oracle_context="ctx")
    assert "step_index" in prompt
    assert "target_paths" in prompt
    assert "acceptance_checks" in prompt


def test_prompt_includes_system_instruction():
    """Prompt includes the required system instruction about JARVIS Trinity."""
    hypothesis = _make_hypothesis()
    prompt = build_design_prompt(hypothesis, oracle_context="ctx")
    assert "JARVIS" in prompt
    assert "Trinity" in prompt


def test_prompt_includes_oracle_context():
    """The oracle_context string is embedded verbatim in the prompt."""
    hypothesis = _make_hypothesis()
    oracle_ctx = "FileNeighborhood: plan.py imports from hypothesis.py"
    prompt = build_design_prompt(hypothesis, oracle_context=oracle_ctx)
    assert "FileNeighborhood" in prompt


def test_prompt_includes_codebase_context_section():
    """Prompt has a CODEBASE CONTEXT section heading."""
    hypothesis = _make_hypothesis()
    prompt = build_design_prompt(hypothesis, oracle_context="ctx")
    assert "CODEBASE" in prompt or "CONTEXT" in prompt


def test_prompt_returns_string():
    """build_design_prompt always returns a str."""
    hypothesis = _make_hypothesis()
    result = build_design_prompt(hypothesis, oracle_context="")
    assert isinstance(result, str)


def test_prompt_is_nonempty():
    """Prompt is non-empty even with minimal inputs."""
    hypothesis = _make_hypothesis()
    result = build_design_prompt(hypothesis, oracle_context="")
    assert len(result) > 100


# ---------------------------------------------------------------------------
# test_json_schema_has_required_fields
# ---------------------------------------------------------------------------


def test_json_schema_has_required_fields():
    """ARCHITECTURAL_PLAN_JSON_SCHEMA string representation contains key field names."""
    schema_str = json.dumps(ARCHITECTURAL_PLAN_JSON_SCHEMA)
    assert "steps" in schema_str
    assert "acceptance_checks" in schema_str


def test_json_schema_is_dict():
    """ARCHITECTURAL_PLAN_JSON_SCHEMA is a dict."""
    assert isinstance(ARCHITECTURAL_PLAN_JSON_SCHEMA, dict)


def test_json_schema_has_title():
    """Schema has a title field."""
    assert "title" in ARCHITECTURAL_PLAN_JSON_SCHEMA


def test_json_schema_has_step_index():
    """Schema str contains step_index field."""
    schema_str = json.dumps(ARCHITECTURAL_PLAN_JSON_SCHEMA)
    assert "step_index" in schema_str


def test_json_schema_has_target_paths():
    """Schema str contains target_paths field."""
    schema_str = json.dumps(ARCHITECTURAL_PLAN_JSON_SCHEMA)
    assert "target_paths" in schema_str


def test_json_schema_has_intent_kind():
    """Schema str contains intent_kind field."""
    schema_str = json.dumps(ARCHITECTURAL_PLAN_JSON_SCHEMA)
    assert "intent_kind" in schema_str


def test_json_schema_has_check_id():
    """Schema str contains check_id field."""
    schema_str = json.dumps(ARCHITECTURAL_PLAN_JSON_SCHEMA)
    assert "check_id" in schema_str


def test_json_schema_has_check_kind():
    """Schema str contains check_kind field."""
    schema_str = json.dumps(ARCHITECTURAL_PLAN_JSON_SCHEMA)
    assert "check_kind" in schema_str


def test_json_schema_has_depends_on():
    """Schema str contains depends_on field."""
    schema_str = json.dumps(ARCHITECTURAL_PLAN_JSON_SCHEMA)
    assert "depends_on" in schema_str


def test_json_schema_has_repos_affected():
    """Schema str contains repos_affected field."""
    schema_str = json.dumps(ARCHITECTURAL_PLAN_JSON_SCHEMA)
    assert "repos_affected" in schema_str


def test_json_schema_has_description():
    """Schema str contains description field."""
    schema_str = json.dumps(ARCHITECTURAL_PLAN_JSON_SCHEMA)
    assert "description" in schema_str


def test_json_schema_steps_is_array():
    """Schema defines steps as an array type."""
    schema_str = json.dumps(ARCHITECTURAL_PLAN_JSON_SCHEMA)
    assert "array" in schema_str


def test_build_design_prompt_custom_max_steps_reflected():
    """When max_steps=3 is passed, the value 3 appears in the generated prompt."""
    hypothesis = _make_hypothesis()
    prompt = build_design_prompt(hypothesis, oracle_context="ctx", max_steps=3)
    assert "3" in prompt


def test_prompt_mentions_json_output():
    """Prompt instructs the model to return JSON."""
    hypothesis = _make_hypothesis()
    prompt = build_design_prompt(hypothesis, oracle_context="ctx")
    prompt_lower = prompt.lower()
    assert "json" in prompt_lower
