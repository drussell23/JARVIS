"""Slice 229 — exploration-floor driven route elevation (pool expansion).

ROOT CAUSE (live soak GOAL-001::file-00, layer 5): the elite agentic models
(Kimi-K2.6 / DeepSeek-V4-Pro / GLM-5.1 — all promoted=True, NOT quarantined)
live on the COMPLEX route's model pool. file-00 is classified
``task_complexity: simple`` -> STANDARD route -> its pool is
[Qwen-397B, Qwen-35B-A3B, DeepSeek-V4-Flash]. When Qwen-397B drifts on
tool-call JSON, the walk falls to the 3B-active weaklings that cannot drive a
tool loop -> exploration_insufficient -> generation_failed. The capable models
were never reachable — a routing starvation, not a provider ceiling.

FIX: when the op faces the Iron Gate exploration floor (the SAME Slice-226
predicate), the dispatch pool is elevated: the COMPLEX route's pool (already
ranked agentic-first by active-param scoring + family preference) is PREPENDED
to the route's own pool, deduped. No model names in code — the elite pool is
whatever the classifier ranked into COMPLEX. Gated
JARVIS_ROUTE_ELEVATION_ENABLED default-TRUE; OFF = byte-identical legacy pool.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.provider_topology import (
    elevate_pool_for_exploration,
)


STANDARD = ("Qwen/Qwen3.5-397B-A17B-FP8", "Qwen/Qwen3.5-35B-A3B-FP8",
            "deepseek-ai/DeepSeek-V4-Flash")
COMPLEX = ("moonshotai/Kimi-K2.6", "deepseek-ai/DeepSeek-V4-Pro",
           "zai-org/GLM-5.1-FP8", "Qwen/Qwen3.5-397B-A17B-FP8")


def test_elevation_prepends_elite_pool_deduped(monkeypatch):
    monkeypatch.setenv("JARVIS_ROUTE_ELEVATION_ENABLED", "1")
    out = elevate_pool_for_exploration(STANDARD, COMPLEX, demands_tools=True)
    # elites first (their own ranked order), then remaining standard, no dupes
    assert out[:4] == COMPLEX
    assert out[4:] == ("Qwen/Qwen3.5-35B-A3B-FP8", "deepseek-ai/DeepSeek-V4-Flash")
    assert len(out) == len(set(out))


def test_no_demand_is_identity(monkeypatch):
    monkeypatch.setenv("JARVIS_ROUTE_ELEVATION_ENABLED", "1")
    out = elevate_pool_for_exploration(STANDARD, COMPLEX, demands_tools=False)
    assert out == STANDARD


def test_master_off_is_identity(monkeypatch):
    monkeypatch.setenv("JARVIS_ROUTE_ELEVATION_ENABLED", "0")
    out = elevate_pool_for_exploration(STANDARD, COMPLEX, demands_tools=True)
    assert out == STANDARD


def test_empty_elite_pool_is_identity(monkeypatch):
    monkeypatch.setenv("JARVIS_ROUTE_ELEVATION_ENABLED", "1")
    out = elevate_pool_for_exploration(STANDARD, (), demands_tools=True)
    assert out == STANDARD


def test_empty_base_pool_gets_elites(monkeypatch):
    monkeypatch.setenv("JARVIS_ROUTE_ELEVATION_ENABLED", "1")
    out = elevate_pool_for_exploration((), COMPLEX, demands_tools=True)
    assert out == COMPLEX


def test_never_raises_on_garbage(monkeypatch):
    monkeypatch.setenv("JARVIS_ROUTE_ELEVATION_ENABLED", "1")
    out = elevate_pool_for_exploration(None, None, demands_tools=True)  # type: ignore[arg-type]
    assert out == ()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
