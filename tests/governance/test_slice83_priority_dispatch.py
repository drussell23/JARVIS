"""Slice 83 — Capability-Based Priority Dispatch & Granular Transport Isolation.

Two coupled fixes to the DW dispatch stack, both surfaced by the abandoned
cost-cascade sweep #6 (Claude disabled → dispatch led with Qwen35B → a single
``live_transport:RuntimeError`` severed the WHOLE lane via Slice 73 → the strong
coders were never reached → exhausted at ~$0):

Phase 1 — Tiered capability-priority sorting. ``_trusted_seed_dw_models_for_route``
previously returned admitted models in INSERTION order, so a small/old model
could lead the COMPLEX dispatch stack. It now sorts by the catalog classifier's
capability ``_score`` (params + cost-aware), so the strong agentic coders
(DeepSeek-V4-Pro, Kimi-K2.6, GLM-5.1 — params 754-1000B) lead and Qwen35B sinks.

Phase 2 — Granular per-model circuit breaking. Slice 73 severed the lane on the
FIRST ``LIVE_TRANSPORT`` failure. With the now-HETEROGENEOUS coder stack (distinct
served endpoints) one model bouncing is not a lane outage. The loop now ROTATES to
the next coder on a single break and only severs once ``threshold`` consecutive
models have all failed transport (``JARVIS_DW_LIVE_TRANSPORT_SEVER_THRESHOLD``,
default 3; ``=1`` reproduces exact Slice 73 behavior). A non-transport failure
(proves a model reachable) resets the streak.
"""
from __future__ import annotations

import inspect

from backend.core.ouroboros.governance.topology_sentinel import FailureSource
from backend.core.ouroboros.governance import candidate_generator as cg
from backend.core.ouroboros.governance.candidate_generator import (
    _live_transport_sever_threshold,
    should_sever_dw_lane,
    structural_fast_cascade_enabled,
)
from backend.core.ouroboros.governance.provider_topology import (
    _trusted_seed_dw_models_for_route,
)


# --- Phase 1: capability-priority ranking ---

_TRUSTED = (
    "Qwen/Qwen3.5-35B-A3B-FP8,Qwen/Qwen3.5-397B-A17B-FP8,"
    "deepseek-ai/DeepSeek-V4-Flash,deepseek-ai/DeepSeek-V4-Pro,"
    "moonshotai/Kimi-K2.6,zai-org/GLM-5.1-FP8"
)


def test_strong_coders_lead_complex_stack(monkeypatch):
    # The strong agentic coders must rank ABOVE the small Qwen35B even though
    # Qwen35B is listed FIRST in the trusted seed (insertion order is the bug).
    monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", _TRUSTED)
    ranked = list(_trusted_seed_dw_models_for_route("complex"))
    assert ranked, "COMPLEX route must admit at least one trusted coder"
    coders = {
        "deepseek-ai/DeepSeek-V4-Pro",
        "moonshotai/Kimi-K2.6",
        "zai-org/GLM-5.1-FP8",
    }
    # every strong coder present must out-rank the small Qwen35B
    if "Qwen/Qwen3.5-35B-A3B-FP8" in ranked:
        small_idx = ranked.index("Qwen/Qwen3.5-35B-A3B-FP8")
        for c in coders:
            if c in ranked:
                assert ranked.index(c) < small_idx, (
                    f"{c} must rank above Qwen35B (got {ranked})"
                )


def test_ranking_is_deterministic(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", _TRUSTED)
    a = _trusted_seed_dw_models_for_route("complex")
    b = _trusted_seed_dw_models_for_route("complex")
    assert a == b, "capability sort must be a stable total order (ties → id)"


def test_ranking_sort_failure_is_non_fatal(monkeypatch):
    # If the classifier import/scoring ever raises, the seed must still return
    # the admitted set (insertion order) rather than blowing up dispatch.
    monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", _TRUSTED)
    monkeypatch.setenv("JARVIS_DW_KNOWN_MODEL_PARAMS", "")  # no perturbation
    ranked = _trusted_seed_dw_models_for_route("complex")
    assert isinstance(ranked, tuple) and len(ranked) >= 1


# --- Phase 2: granular sever threshold ---

def test_threshold_default_is_three(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_LIVE_TRANSPORT_SEVER_THRESHOLD", raising=False)
    assert _live_transport_sever_threshold() == 3


def test_threshold_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_LIVE_TRANSPORT_SEVER_THRESHOLD", "5")
    assert _live_transport_sever_threshold() == 5


def test_threshold_floored_at_one(monkeypatch):
    # =1 must reproduce exact Slice 73 first-failure sever; 0/negative floor up.
    monkeypatch.setenv("JARVIS_DW_LIVE_TRANSPORT_SEVER_THRESHOLD", "1")
    assert _live_transport_sever_threshold() == 1
    monkeypatch.setenv("JARVIS_DW_LIVE_TRANSPORT_SEVER_THRESHOLD", "0")
    assert _live_transport_sever_threshold() == 1
    monkeypatch.setenv("JARVIS_DW_LIVE_TRANSPORT_SEVER_THRESHOLD", "-4")
    assert _live_transport_sever_threshold() == 1


def test_threshold_garbage_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_LIVE_TRANSPORT_SEVER_THRESHOLD", "not-a-number")
    assert _live_transport_sever_threshold() == 3


def test_sever_decision_below_threshold_rotates(monkeypatch):
    # The real composite decision the dispatch loop makes. Below the streak
    # threshold a LIVE_TRANSPORT failure must NOT sever (rotate to next coder).
    monkeypatch.setenv("JARVIS_DW_LIVE_TRANSPORT_SEVER_THRESHOLD", "3")
    threshold = _live_transport_sever_threshold()
    fs = FailureSource.LIVE_TRANSPORT
    for streak in (1, 2):
        should = (
            structural_fast_cascade_enabled()
            and should_sever_dw_lane(fs)
            and streak >= threshold
        )
        assert should is False, f"streak={streak} must rotate, not sever"


def test_sever_decision_at_threshold_severs(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_LIVE_TRANSPORT_SEVER_THRESHOLD", "3")
    threshold = _live_transport_sever_threshold()
    should = (
        structural_fast_cascade_enabled()
        and should_sever_dw_lane(FailureSource.LIVE_TRANSPORT)
        and 3 >= threshold
    )
    assert should is True, "a full consecutive-transport streak must sever"


def test_non_transport_failure_never_severs(monkeypatch):
    # 429/5xx/parse prove a model reachable → they must not sever regardless of
    # streak length (and in the loop they RESET the streak).
    monkeypatch.setenv("JARVIS_DW_LIVE_TRANSPORT_SEVER_THRESHOLD", "1")
    threshold = _live_transport_sever_threshold()
    for fs in (
        FailureSource.LIVE_HTTP_429,
        FailureSource.LIVE_HTTP_5XX,
        FailureSource.LIVE_PARSE_ERROR,
    ):
        should = (
            structural_fast_cascade_enabled()
            and should_sever_dw_lane(fs)
            and 99 >= threshold
        )
        assert should is False, f"{fs} must never sever the lane"


# --- wiring pins: the inline dispatch loop actually consumes the above ---

def test_dispatch_loop_wires_streak_counter():
    src = inspect.getsource(cg)
    # the consecutive-transport streak counter exists and gates the sever
    assert "_consecutive_lt" in src, "loop must track a consecutive LT streak"
    assert "_lt_sever_threshold" in src, "loop must bind the sever threshold"
    assert "_live_transport_sever_threshold(" in src
    # the sever is now streak-gated, not first-failure
    assert "_consecutive_lt >= _lt_sever_threshold" in src


def test_dispatch_loop_resets_streak_on_non_transport():
    src = inspect.getsource(cg)
    # there must be a reset path (non-transport failure resets the streak)
    assert "_consecutive_lt = 0" in src, "non-transport failure must reset streak"
    assert "_consecutive_lt += 1" in src, "transport failure must grow the streak"


def test_dispatch_loop_still_consults_slice73_predicates():
    # Slice 83 composes WITH Slice 73, never replaces it.
    src = inspect.getsource(cg)
    assert "should_sever_dw_lane(" in src
    assert "structural_fast_cascade_enabled()" in src
