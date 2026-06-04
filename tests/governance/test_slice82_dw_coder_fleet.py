"""Slice 82 — DW agentic-coder fleet: make the strong, cheap DW models eligible.

Cost root cause (PRD §50 spend audit): on SWE-bench Pro, Claude carried ~99.9% of
spend because the only TRUSTED + ELIGIBLE DW models were the older Qwen3.5 family.
DW also serves Claude-class agentic coders — GLM-5.1 (SWE-bench Pro 58.4%, beats
Opus 4.6), Kimi-K2.6 (58.6%), DeepSeek-V4-Pro (Verified 80.6%) — at 5-25× lower
cost, and they're live on the account. But their model_ids carry NO parseable
``\\d+B`` token, so `parse_parameter_count` returned None and the COMPLEX route's
`min_params_b=30` gate REJECTED them — they could never carry GENERATE.

Slice 82 enriches the catalog METADATA (params + pricing) so the EXISTING dynamic
catalog → gate → rank → trusted-seed machinery selects them. No hardcoded routing;
the selection stays dynamic + env-extensible.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.dw_catalog_client import (
    parse_parameter_count,
)
from backend.core.ouroboros.governance.pricing_oracle import resolve_pricing


# --- params enrichment (the gate-blocker fix) ---

@pytest.mark.parametrize("model_id,expected_min", [
    ("zai-org/GLM-5.1-FP8", 700.0),
    ("moonshotai/Kimi-K2.6", 1000.0),
    ("deepseek-ai/DeepSeek-V4-Pro", 1000.0),
    ("deepseek-ai/DeepSeek-V4-Flash", 30.0),
])
def test_coder_params_enriched_above_complex_floor(model_id, expected_min):
    pb = parse_parameter_count(model_id)
    assert pb is not None, f"{model_id} must resolve a param count"
    assert pb >= 30.0, f"{model_id} must clear the COMPLEX min_params_b=30 gate"
    assert pb >= expected_min


def test_regex_path_unchanged_for_sized_ids():
    # the curated map must NOT perturb ids that already parse
    assert parse_parameter_count("Qwen/Qwen3.5-397B-A17B-FP8") == 397.0
    assert parse_parameter_count("google/gemma-4-31B-it") == 31.0
    assert parse_parameter_count("Qwen/Qwen3.5-9B") == 9.0


def test_unknown_unsized_model_stays_none():
    # conservative: a model neither sized nor curated stays None (quarantine)
    assert parse_parameter_count("acme/MysteryModel-v1") is None


def test_known_params_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_KNOWN_MODEL_PARAMS", "mystery-coder:512")
    assert parse_parameter_count("acme/Mystery-Coder-Pro") == 512.0
    # operator entry wins over the curated default
    monkeypatch.setenv("JARVIS_DW_KNOWN_MODEL_PARAMS", "glm-5:999")
    assert parse_parameter_count("zai-org/GLM-5.1-FP8") == 999.0


# --- pricing enrichment (cost-aware ranking) ---

@pytest.mark.parametrize("model_id", [
    "zai-org/GLM-5.1-FP8",
    "moonshotai/Kimi-K2.6",
    "deepseek-ai/DeepSeek-V4-Pro",
])
def test_coder_pricing_resolved_and_far_below_claude(model_id):
    pr = resolve_pricing(model_id)
    assert pr is not None, f"{model_id} must resolve pricing"
    price_out = pr[1]
    assert price_out is not None and price_out > 0
    # every DW coder is dramatically cheaper than Claude Opus (~$15-75/M out)
    assert price_out < 5.0, f"{model_id} out-price ${price_out} should be << Claude"


# --- the COMPLEX route now admits the coders ---

def test_coders_admitted_by_complex_route_gate():
    from backend.core.ouroboros.governance.dw_catalog_client import ModelCard
    from backend.core.ouroboros.governance.dw_catalog_classifier import (
        gate_for_route,
    )
    gate = gate_for_route("complex")
    for mid in ("zai-org/GLM-5.1-FP8", "moonshotai/Kimi-K2.6",
                "deepseek-ai/DeepSeek-V4-Pro"):
        card = ModelCard(
            model_id=mid,
            family=mid.split("/", 1)[0],
            parameter_count_b=parse_parameter_count(mid),
            context_window=131072,
            pricing_in_per_m_usd=(resolve_pricing(mid) or (None, None))[0],
            pricing_out_per_m_usd=(resolve_pricing(mid) or (None, None))[1],
            supports_streaming=True,
            raw_metadata_json="{}",
        )
        assert gate.admits(card) is True, f"COMPLEX route must admit {mid}"
