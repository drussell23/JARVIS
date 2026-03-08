# tests/governance/self_dev/test_op_context_phase2b.py
"""Tests for Phase 2B op_context changes."""
from backend.core.ouroboros.governance.op_context import GenerationResult


def test_generation_result_model_id_defaults_to_empty_string():
    """model_id has backward-compatible default."""
    gr = GenerationResult(
        candidates=(),
        provider_name="gcp-jprime",
        generation_duration_s=0.5,
    )
    assert gr.model_id == ""


def test_generation_result_model_id_can_be_set():
    """model_id can be set explicitly."""
    gr = GenerationResult(
        candidates=(),
        provider_name="gcp-jprime",
        generation_duration_s=0.5,
        model_id="llama-3.3-70b",
    )
    assert gr.model_id == "llama-3.3-70b"


def test_generation_result_is_still_frozen():
    """GenerationResult remains immutable."""
    import pytest
    gr = GenerationResult(candidates=(), provider_name="p", generation_duration_s=0.1)
    with pytest.raises((AttributeError, TypeError)):
        gr.model_id = "changed"  # type: ignore[misc]
