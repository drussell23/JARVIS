"""Slice 228 — MoE active-parameter cognitive parser + active-aware ranking.

ROOT CAUSE (live soak GOAL-001::file-00): the catalog ranker reads TOTAL params
from the model id ("first match wins": ``Qwen3.5-35B-A3B → 35``), so it rated a
3B-ACTIVE MoE as a mid-tier 35B model and routed agentic (tool-loop) ops to it —
where it chokes on tool execution. Total params is the wrong capability metric
for MoE models; ACTIVE params (the ``A<N>B`` token) is what predicts agentic
tool-use ability.

FIX (no hardcoded model names — metadata-derived, same pattern as the existing
total-param parse): parse the ``A<N>B`` active-parameter token into a new
``active_parameter_count_b`` ModelCard field (dense / no-token → falls back to
total), and score capability on ACTIVE params. This demotes the 3B-active
Qwen-35B and elevates the high-active agentic heavyweights — generalizing to ANY
future large-active model with zero code change. Gated
JARVIS_DW_ACTIVE_PARAM_SCORING_ENABLED default-TRUE.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.dw_catalog_client import (
    ModelCard,
    parse_active_parameter_count,
    parse_parameter_count,
)


# ── the active-param parser (metadata-derived, no hardcoded model names) ────

def test_parses_active_token_small_moe():
    assert parse_active_parameter_count("Qwen/Qwen3.5-35B-A3B-FP8") == 3.0


def test_parses_active_token_large_moe():
    assert parse_active_parameter_count("Qwen/Qwen3.5-397B-A17B-FP8") == 17.0


def test_parses_active_token_nemotron():
    assert parse_active_parameter_count(
        "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-NVFP4") == 55.0


def test_dense_model_has_no_active_token():
    # Dense models carry no A<N>B token; the parser returns None (the ModelCard
    # then falls back to total params).
    assert parse_active_parameter_count("google/gemma-4-31B-it") is None
    assert parse_active_parameter_count("Qwen/Qwen3-14B-FP8") is None


def test_total_param_parse_unchanged():
    # The existing total-param parse must stay byte-identical (regression).
    assert parse_parameter_count("Qwen/Qwen3.5-35B-A3B-FP8") == 35.0
    assert parse_parameter_count("Qwen/Qwen3.5-397B-A17B-FP8") == 397.0


# ── ModelCard carries the active count (falls back to total when dense) ─────

def test_modelcard_active_from_token():
    card = ModelCard.from_api_dict({"id": "Qwen/Qwen3.5-35B-A3B-FP8"})
    assert card is not None
    assert card.active_parameter_count_b == 3.0
    assert card.parameter_count_b == 35.0  # total still available


def test_modelcard_active_falls_back_to_total_for_dense():
    card = ModelCard.from_api_dict({"id": "google/gemma-4-31B-it"})
    assert card is not None
    # dense: no A-token → active mirrors total
    assert card.active_parameter_count_b == card.parameter_count_b == 31.0


# ── active-aware scoring demotes the 3B-active MoE on a capability route ─────

def test_active_scoring_demotes_small_active_moe(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_ACTIVE_PARAM_SCORING_ENABLED", "1")
    from backend.core.ouroboros.governance.dw_catalog_classifier import (
        _score, _ranking_weights,
    )
    w = _ranking_weights()
    small = ModelCard.from_api_dict({"id": "Qwen/Qwen3.5-35B-A3B-FP8"})   # 3 active
    big = ModelCard.from_api_dict({"id": "Qwen/Qwen3.5-397B-A17B-FP8"})   # 17 active
    assert small is not None and big is not None
    s_big = _score(big, w, {}, prefer_cheap=False)
    s_small = _score(small, w, {}, prefer_cheap=False)
    assert s_big > s_small, (
        f"high-active model must outrank low-active on a capability route; "
        f"got big={s_big} small={s_small}")


def test_active_vs_total_changes_small_moe_score(monkeypatch):
    """Same card, only the flag differs — the capability term must drop from
    total(35) to active(3) when the governor is on."""
    from backend.core.ouroboros.governance.dw_catalog_classifier import (
        _score, _ranking_weights,
    )
    w = _ranking_weights()
    small = ModelCard.from_api_dict({"id": "Qwen/Qwen3.5-35B-A3B-FP8"})
    assert small is not None
    monkeypatch.setenv("JARVIS_DW_ACTIVE_PARAM_SCORING_ENABLED", "0")
    s_total = _score(small, w, {}, prefer_cheap=False)   # total 35
    monkeypatch.setenv("JARVIS_DW_ACTIVE_PARAM_SCORING_ENABLED", "1")
    s_active = _score(small, w, {}, prefer_cheap=False)   # active 3
    # capability term: total(35)/10 - active(3)/10 = 3.2 higher under legacy
    assert s_total - s_active == pytest.approx(3.2, abs=0.01)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
