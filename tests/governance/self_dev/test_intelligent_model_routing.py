# tests/governance/self_dev/test_intelligent_model_routing.py
"""Tests for intelligent model routing — 32B upgrade, schema_capability, SAI downgrade."""
import pytest
from unittest.mock import MagicMock

from backend.core.ouroboros.governance.brain_selector import (
    BrainSelector,
    BrainSelectionResult,
    TaskComplexity,
)
from backend.core.ouroboros.governance.resource_monitor import ResourceSnapshot


def _snap() -> ResourceSnapshot:
    """Minimal resource snapshot for testing."""
    return ResourceSnapshot(
        ram_percent=50.0,
        cpu_percent=20.0,
        event_loop_latency_ms=5.0,
        disk_io_busy=False,
        ram_available_gb=8.0,
        platform_arch="arm64",
        sampled_monotonic_ns=0,
    )


# ── Complexity → Brain mapping (32B upgrade) ─────────────────────────────────


def test_trivial_routes_to_phi3():
    bs = BrainSelector()
    result = bs.select("append a comment line", ("docs/readme.md",), _snap())
    assert result.brain_id == "phi3_lightweight"


def test_light_routes_to_qwen_coder_7b():
    bs = BrainSelector()
    result = bs.select("fix the bug in auth module", ("auth.py",), _snap())
    assert result.brain_id == "qwen_coder"
    assert "7b" in result.model_name


def test_heavy_code_routes_to_qwen_coder_32b():
    bs = BrainSelector()
    result = bs.select(
        "refactor the entire authentication system",
        ("auth.py", "session.py", "middleware.py"),
        _snap(),
    )
    assert result.brain_id == "qwen_coder_32b"
    assert "32b" in result.model_name


def test_complex_routes_to_qwen_coder_32b():
    bs = BrainSelector()
    result = bs.select(
        "analyze codebase and redesign the architecture",
        ("core/engine.py", "core/router.py", "core/db.py",
         "api/endpoints.py", "api/models.py", "tests/test_e2e.py"),
        _snap(),
    )
    assert result.brain_id == "qwen_coder_32b"


# ── schema_capability ────────────────────────────────────────────────────────


def test_32b_brain_has_diff_schema_capability():
    bs = BrainSelector()
    result = bs.select(
        "refactor the entire module",
        ("a.py", "b.py", "c.py"),
        _snap(),
    )
    assert result.brain_id == "qwen_coder_32b"
    assert result.schema_capability == "full_content_and_diff"


def test_7b_brain_has_full_content_only_schema():
    bs = BrainSelector()
    result = bs.select("fix the bug in login", ("login.py",), _snap())
    # Light task → qwen_coder (7B)
    assert result.brain_id == "qwen_coder"
    assert result.schema_capability == "full_content_only"


def test_trivial_brain_has_full_content_only_schema():
    bs = BrainSelector()
    result = bs.select("append a single line", ("docs/readme.md",), _snap())
    assert result.brain_id == "phi3_lightweight"
    assert result.schema_capability == "full_content_only"


# ── narration ────────────────────────────────────────────────────────────────


def test_narration_on_brain_selection_result():
    result = BrainSelectionResult(
        brain_id="qwen_coder_32b",
        model_name="qwen-2.5-coder-32b",
        fallback_model="qwen-2.5-coder-14b",
        routing_reason="cai_intent_heavy_refactor",
        task_complexity="heavy_code",
        schema_capability="full_content_and_diff",
    )
    narr = result.narration()
    assert "Qwen" in narr
    assert "32" in narr
    assert "G-C-P" in narr


def test_narration_queued():
    result = BrainSelectionResult(
        brain_id="queued",
        model_name="queued",
        fallback_model="queued",
        routing_reason="cost_gate_triggered_queue",
        task_complexity="heavy_code",
        provider_tier="queued",
    )
    narr = result.narration()
    assert "queued" in narr.lower()
    assert "budget" in narr.lower()


# ── fallback chain ───────────────────────────────────────────────────────────


def test_32b_fallback_is_14b():
    bs = BrainSelector()
    result = bs.select(
        "refactor the entire module",
        ("a.py", "b.py", "c.py"),
        _snap(),
    )
    assert result.brain_id == "qwen_coder_32b"
    assert "14b" in result.fallback_model


# ── cost gate still works ────────────────────────────────────────────────────


def test_cost_gate_queues_heavy_task_when_budget_exceeded():
    bs = BrainSelector()
    # Simulate exceeding budget
    bs._daily_spend_gcp = 0.60
    bs._cost_date = __import__("time").strftime("%Y-%m-%d")
    result = bs.select(
        "refactor the entire auth system",
        ("a.py", "b.py", "c.py"),
        _snap(),
    )
    assert result.provider_tier == "queued"
