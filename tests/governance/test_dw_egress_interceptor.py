from __future__ import annotations
import pytest
from backend.core.ouroboros.governance import dw_egress_interceptor as egi


def _body(chars: int, model="m", reasoning="none"):
    return {"model": model, "messages": [{"role": "user", "content": "x" * chars}], "reasoning_effort": reasoning}


def test_enabled_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", raising=False)
    assert egi.egress_interceptor_enabled() is True


def test_estimate_chars():
    assert egi.estimate_body_chars(_body(100)) >= 100


def test_under_ceiling_passes(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_EGRESS_MAX_CHARS", "1000")
    egi.assert_egress_weight(_body(500), "anymodel")  # no raise


def test_over_ceiling_raises_with_math(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_EGRESS_MAX_CHARS", "1000")
    with pytest.raises(egi.LocalEgressOverweightError) as ei:
        egi.assert_egress_weight(_body(5000), "anymodel")
    e = ei.value
    assert e.max_allowed_size == 1000 and e.attempted_size >= 5000
    assert e.required_compression_ratio >= 5.0 and e.model == "anymodel"


def test_sanitize_unknown_model_passthrough():
    b = _body(10, model="totally-unknown", reasoning="none")
    assert egi.sanitize_egress_body(b, "totally-unknown") == b


def test_sanitize_gpt_oss_floors_reasoning():
    b = _body(10, model="openai/gpt-oss-120b", reasoning="none")
    out = egi.sanitize_egress_body(b, "openai/gpt-oss-120b")
    assert out["reasoning_effort"] != "none"  # floored via reused logic


def test_failsoft_bad_body_never_raises_unexpected():
    egi.assert_egress_weight({"messages": None}, "m")   # estimate->0->no block
    assert egi.sanitize_egress_body({"messages": None}, "m") == {"messages": None}


def test_overweight_error_ratio_zero_guard():
    e = egi.LocalEgressOverweightError(attempted_size=100, max_allowed_size=0, model="m")
    assert e.required_compression_ratio >= 1.0  # no ZeroDivisionError
