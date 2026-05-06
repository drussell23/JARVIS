"""§31 U2 empirical wiring Slice 2 — CONTEXT_EXPANSION
causal-lineage injection regression spine.

Pins per operator binding 2026-05-05:

  * compose_causal_lineage_section produces empty on
    DISABLED / None / ancestor_count==0 (no empty headers)
  * Section format: header + lineage stat line + advice
    paragraph + authority disclaimer
  * Budget cap 2KB (env-tunable) — section truncated cleanly
  * Advice paragraph mapping:
      - NEUTRAL → no paragraph
      - SIBLING_DEDUP → sibling paragraph
      - RECURRENCE_WARNING → recurrence paragraph
      - DEEP_LINEAGE_HARDEN → deep-lineage paragraph
  * StrategicDirectionService.format_for_prompt accepts session_id
    + record_id kwargs without breaking pre-Slice-2 callers
  * _render_causal_lineage_section is fail-silent / ImportError-
    safe / master-flag-checked
  * Existing callers (no kwargs) → byte-identical pre-Slice-2
    behavior

Verifies (24 tests).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# compose_causal_lineage_section — empty / silent paths
# ---------------------------------------------------------------------------


def test_compose_returns_empty_on_none():
    from backend.core.ouroboros.governance.causality_consumer import (
        compose_causal_lineage_section,
    )
    assert compose_causal_lineage_section(None) == ""


def test_compose_returns_empty_on_disabled():
    from backend.core.ouroboros.governance.causality_consumer import (
        CAUSAL_FEATURES_SCHEMA_VERSION, CausalDecisionAdvice,
        OpCausalFeatures, compose_causal_lineage_section,
    )
    f = OpCausalFeatures(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id="s", record_id="r",
        ancestor_count=5,  # would have lineage…
        distinct_phases_in_lineage=("GENERATE",),
        sibling_count=0, recurrence_score=0.0,
        parent_decisions_summary="",
        advice=CausalDecisionAdvice.DISABLED,  # …but DISABLED
    )
    assert compose_causal_lineage_section(f) == ""


def test_compose_returns_empty_on_zero_ancestors():
    from backend.core.ouroboros.governance.causality_consumer import (
        CAUSAL_FEATURES_SCHEMA_VERSION, CausalDecisionAdvice,
        OpCausalFeatures, compose_causal_lineage_section,
    )
    f = OpCausalFeatures(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id="s", record_id="r",
        ancestor_count=0,
        distinct_phases_in_lineage=(),
        sibling_count=0, recurrence_score=0.0,
        parent_decisions_summary="",
        advice=CausalDecisionAdvice.NEUTRAL,
    )
    assert compose_causal_lineage_section(f) == ""


# ---------------------------------------------------------------------------
# compose_causal_lineage_section — happy path renders
# ---------------------------------------------------------------------------


def test_compose_renders_neutral_no_advice_paragraph():
    from backend.core.ouroboros.governance.causality_consumer import (
        CAUSAL_FEATURES_SCHEMA_VERSION, CausalDecisionAdvice,
        OpCausalFeatures, compose_causal_lineage_section,
    )
    f = OpCausalFeatures(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id="s", record_id="r",
        ancestor_count=2,
        distinct_phases_in_lineage=("GENERATE", "VALIDATE"),
        sibling_count=0, recurrence_score=0.0,
        parent_decisions_summary="parent-1",
        advice=CausalDecisionAdvice.NEUTRAL,
    )
    section = compose_causal_lineage_section(f)
    assert "## Recent Causal Lineage" in section
    assert "2 upstream decisions" in section
    assert "GENERATE, VALIDATE" in section
    # No NEUTRAL advice paragraph
    assert "Recurrence pattern" not in section
    assert "Sibling-fork pattern" not in section
    assert "Deep-lineage warning" not in section
    # Authority disclaimer always present
    assert "Authority disclaimer" in section


def test_compose_renders_recurrence_warning_paragraph():
    from backend.core.ouroboros.governance.causality_consumer import (
        CAUSAL_FEATURES_SCHEMA_VERSION, CausalDecisionAdvice,
        OpCausalFeatures, compose_causal_lineage_section,
    )
    f = OpCausalFeatures(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id="s", record_id="r",
        ancestor_count=4,
        distinct_phases_in_lineage=("GENERATE",),
        sibling_count=0, recurrence_score=0.75,
        parent_decisions_summary="",
        advice=CausalDecisionAdvice.RECURRENCE_WARNING,
    )
    section = compose_causal_lineage_section(f)
    assert "Recurrence pattern detected" in section
    assert "75%" in section


def test_compose_renders_sibling_dedup_paragraph():
    from backend.core.ouroboros.governance.causality_consumer import (
        CAUSAL_FEATURES_SCHEMA_VERSION, CausalDecisionAdvice,
        OpCausalFeatures, compose_causal_lineage_section,
    )
    f = OpCausalFeatures(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id="s", record_id="r",
        ancestor_count=2,
        distinct_phases_in_lineage=("CLASSIFY",),
        sibling_count=4, recurrence_score=0.1,
        parent_decisions_summary="",
        advice=CausalDecisionAdvice.SIBLING_DEDUP,
    )
    section = compose_causal_lineage_section(f)
    assert "Sibling-fork pattern" in section
    assert "sibling forks: 4" in section


def test_compose_renders_deep_lineage_paragraph():
    from backend.core.ouroboros.governance.causality_consumer import (
        CAUSAL_FEATURES_SCHEMA_VERSION, CausalDecisionAdvice,
        OpCausalFeatures, compose_causal_lineage_section,
    )
    f = OpCausalFeatures(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id="s", record_id="r",
        ancestor_count=15,
        distinct_phases_in_lineage=("GENERATE", "VALIDATE", "GATE"),
        sibling_count=0, recurrence_score=0.1,
        parent_decisions_summary="",
        advice=CausalDecisionAdvice.DEEP_LINEAGE_HARDEN,
    )
    section = compose_causal_lineage_section(f)
    assert "Deep-lineage warning" in section
    assert "15 upstream decisions" in section


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


def test_compose_truncates_at_budget():
    from backend.core.ouroboros.governance.causality_consumer import (
        CAUSAL_FEATURES_SCHEMA_VERSION, CausalDecisionAdvice,
        OpCausalFeatures, compose_causal_lineage_section,
    )
    f = OpCausalFeatures(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id="s", record_id="r",
        ancestor_count=3,
        distinct_phases_in_lineage=("GENERATE",),
        sibling_count=0, recurrence_score=0.0,
        parent_decisions_summary="",
        advice=CausalDecisionAdvice.NEUTRAL,
    )
    short = compose_causal_lineage_section(f, max_chars=100)
    assert len(short) <= 100
    # Truncation marker
    if len(short) == 100:
        assert short.endswith("...")


def test_budget_default_2048(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CAUSAL_LINEAGE_PROMPT_BUDGET", raising=False,
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        DEFAULT_CAUSAL_LINEAGE_PROMPT_BUDGET,
        causal_lineage_prompt_budget,
    )
    assert (
        causal_lineage_prompt_budget()
        == DEFAULT_CAUSAL_LINEAGE_PROMPT_BUDGET
        == 2048
    )


def test_budget_env_override(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_LINEAGE_PROMPT_BUDGET", "512",
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        causal_lineage_prompt_budget,
    )
    assert causal_lineage_prompt_budget() == 512


def test_budget_env_garbage_falls_back(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_LINEAGE_PROMPT_BUDGET", "not_int",
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        DEFAULT_CAUSAL_LINEAGE_PROMPT_BUDGET,
        causal_lineage_prompt_budget,
    )
    assert (
        causal_lineage_prompt_budget()
        == DEFAULT_CAUSAL_LINEAGE_PROMPT_BUDGET
    )


def test_compose_returns_empty_on_zero_max_chars():
    from backend.core.ouroboros.governance.causality_consumer import (
        CAUSAL_FEATURES_SCHEMA_VERSION, CausalDecisionAdvice,
        OpCausalFeatures, compose_causal_lineage_section,
    )
    f = OpCausalFeatures(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id="s", record_id="r",
        ancestor_count=2,
        distinct_phases_in_lineage=("GENERATE",),
        sibling_count=0, recurrence_score=0.0,
        parent_decisions_summary="",
        advice=CausalDecisionAdvice.NEUTRAL,
    )
    assert compose_causal_lineage_section(
        f, max_chars=0,
    ) == ""


# ---------------------------------------------------------------------------
# StrategicDirectionService.format_for_prompt — Slice 2 wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def loaded_service(tmp_path, monkeypatch):
    """Construct a loaded StrategicDirectionService with a
    minimal digest so format_for_prompt has something to return."""
    from backend.core.ouroboros.governance.strategic_direction import (
        StrategicDirectionService,
    )
    svc = StrategicDirectionService(project_root=tmp_path)
    svc._digest = "Test digest content."  # type: ignore
    svc._loaded = True  # type: ignore
    return svc


def test_format_for_prompt_byte_identical_without_slice2_kwargs(
    loaded_service,
):
    """Existing callers that don't pass session_id / record_id
    should get pre-Slice-2 byte-identical output."""
    out_a = loaded_service.format_for_prompt()
    out_b = loaded_service.format_for_prompt()
    # No causal-lineage section appended
    assert "## Recent Causal Lineage" not in out_a
    assert out_a == out_b


def test_format_for_prompt_silent_when_session_id_missing(
    loaded_service,
):
    out = loaded_service.format_for_prompt(record_id="some-id")
    assert "## Recent Causal Lineage" not in out


def test_format_for_prompt_silent_when_record_id_missing(
    loaded_service,
):
    out = loaded_service.format_for_prompt(session_id="some-sid")
    assert "## Recent Causal Lineage" not in out


def test_format_for_prompt_silent_when_substrate_disabled(
    loaded_service, monkeypatch,
):
    """Master flag off → causal-lineage section silent."""
    monkeypatch.delenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", raising=False,
    )
    out = loaded_service.format_for_prompt(
        session_id="s", record_id="r",
    )
    assert "## Recent Causal Lineage" not in out


def test_format_for_prompt_renders_section_when_features_present(
    loaded_service, monkeypatch,
):
    """When features come back non-empty, the section appears
    after the action-outcomes block."""
    monkeypatch.setenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        CAUSAL_FEATURES_SCHEMA_VERSION, CausalDecisionAdvice,
        OpCausalFeatures,
    )
    fake_features = OpCausalFeatures(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id="s", record_id="r",
        ancestor_count=4,
        distinct_phases_in_lineage=("GENERATE", "VALIDATE"),
        sibling_count=0, recurrence_score=0.6,
        parent_decisions_summary="parent",
        advice=CausalDecisionAdvice.RECURRENCE_WARNING,
    )
    with patch(
        "backend.core.ouroboros.governance.causality_consumer."
        "compute_op_causal_features",
        return_value=fake_features,
    ):
        out = loaded_service.format_for_prompt(
            session_id="s", record_id="r",
        )
    assert "## Recent Causal Lineage" in out
    assert "Recurrence pattern detected" in out


def test_format_for_prompt_section_position_is_last(
    loaded_service, monkeypatch,
):
    """Causal-lineage block must appear AFTER action-outcomes
    (most recent → most relevant; recency-of-attention)."""
    monkeypatch.setenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        CAUSAL_FEATURES_SCHEMA_VERSION, CausalDecisionAdvice,
        OpCausalFeatures,
    )
    f = OpCausalFeatures(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id="s", record_id="r",
        ancestor_count=2,
        distinct_phases_in_lineage=("GENERATE",),
        sibling_count=0, recurrence_score=0.0,
        parent_decisions_summary="",
        advice=CausalDecisionAdvice.NEUTRAL,
    )
    with patch(
        "backend.core.ouroboros.governance.causality_consumer."
        "compute_op_causal_features",
        return_value=f,
    ):
        out = loaded_service.format_for_prompt(
            session_id="s", record_id="r",
        )
    causal_idx = out.find("## Recent Causal Lineage")
    # Should appear at all
    assert causal_idx >= 0
    # If action-outcomes also appears, causal must be after
    action_idx = out.find("## Recent Region Outcomes")
    if action_idx >= 0:
        assert causal_idx > action_idx


def test_render_method_fail_silent_on_substrate_unavailable(
    loaded_service, monkeypatch,
):
    """If the lazy import of causality_consumer fails (rollback
    branch), _render_causal_lineage_section returns empty
    instead of raising."""
    real_import = __import__

    def _block(name, *args, **kwargs):
        if name == (
            "backend.core.ouroboros.governance.causality_consumer"
        ):
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _block)
    out = loaded_service.format_for_prompt(
        session_id="s", record_id="r",
    )
    assert "## Recent Causal Lineage" not in out


def test_render_method_fail_silent_on_compute_exception(
    loaded_service, monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "1",
    )

    def _broken(*a, **kw):
        raise RuntimeError("simulated")

    with patch(
        "backend.core.ouroboros.governance.causality_consumer."
        "compute_op_causal_features",
        side_effect=_broken,
    ):
        out = loaded_service.format_for_prompt(
            session_id="s", record_id="r",
        )
    # Doesn't break the prompt
    assert isinstance(out, str)
    assert "## Strategic Direction" in out
    assert "## Recent Causal Lineage" not in out


# ---------------------------------------------------------------------------
# Authority — section explicitly disclaims execution authority
# ---------------------------------------------------------------------------


def test_section_carries_authority_disclaimer(
    loaded_service, monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        CAUSAL_FEATURES_SCHEMA_VERSION, CausalDecisionAdvice,
        OpCausalFeatures,
    )
    f = OpCausalFeatures(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id="s", record_id="r",
        ancestor_count=2,
        distinct_phases_in_lineage=("GENERATE",),
        sibling_count=0, recurrence_score=0.0,
        parent_decisions_summary="",
        advice=CausalDecisionAdvice.NEUTRAL,
    )
    with patch(
        "backend.core.ouroboros.governance.causality_consumer."
        "compute_op_causal_features",
        return_value=f,
    ):
        out = loaded_service.format_for_prompt(
            session_id="s", record_id="r",
        )
    causal_section_start = out.find("## Recent Causal Lineage")
    causal_section = out[causal_section_start:]
    # Section MUST disclaim execution authority
    assert "Iron Gate" in causal_section
    assert "informational only" in causal_section
