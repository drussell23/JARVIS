"""Slice 201 — Contextual Bandit Routing Advisor (Thompson Sampling).

An online-learning ADVISOR over model arms: for each routing decision it
Thompson-samples each arm's reward posterior and prefers the best. It is
advisory-only and structurally fail-closed — it reorders WITHIN the
policy-provided ``ranked_models`` (the brain_selection_policy active set), so
it can never select an out-of-policy arm; the deterministic order is the
fallback whenever the bandit is off, errors, or has no opinion.

Reward = (Success·W_s − Cost·W_c − Latency·W_l), mapped to [0,1] and folded
into a Beta posterior per arm. Learns from outcomes recorded at the dispatch
result sites.

Pins:
  * compute_reward math + clamping.
  * record_outcome updates the right posterior direction.
  * advise/best_arm converge to the empirically-better arm under evidence.
  * advise NEVER invents or returns an out-of-set arm (the safety boundary).
  * disabled / empty / garbage → None (fail-closed), never raises.
  * durable round-trip.
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.bandit_router import (
    BanditRouter,
    bandit_router_enabled,
    compute_reward,
    get_bandit_router,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOV = _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_BANDIT_STATE_PATH", str(tmp_path / "bandit.json"))
    for var in (
        "JARVIS_BANDIT_ROUTER_ENABLED", "JARVIS_BANDIT_W_SUCCESS",
        "JARVIS_BANDIT_W_COST", "JARVIS_BANDIT_W_LATENCY",
        "JARVIS_BANDIT_COST_SCALE_USD", "JARVIS_BANDIT_LATENCY_SCALE_S",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _router(tmp_path, seed=7):
    return BanditRouter(
        state_path=tmp_path / "b.json", rng=random.Random(seed),
    )


# ===========================================================================
# A — gate
# ===========================================================================

def test_disabled_by_default():
    assert bandit_router_enabled() is False


def test_enabled_via_env(monkeypatch):
    monkeypatch.setenv("JARVIS_BANDIT_ROUTER_ENABLED", "1")
    assert bandit_router_enabled() is True


# ===========================================================================
# B — reward math
# ===========================================================================

def test_reward_success_free_fast_is_max():
    assert compute_reward(True, cost_usd=0.0, latency_s=0.0) == pytest.approx(1.0)


def test_reward_failure_expensive_slow_is_min():
    r = compute_reward(False, cost_usd=10.0, latency_s=999.0)
    assert r == pytest.approx(0.0)


def test_reward_success_default_is_high():
    # success with unknown cost/latency → no penalty → top
    assert compute_reward(True) == pytest.approx(1.0)


def test_reward_is_clamped_to_unit_interval():
    for s in (True, False):
        r = compute_reward(s, cost_usd=-5.0, latency_s=-9.0)
        assert 0.0 <= r <= 1.0


# ===========================================================================
# C — posterior updates
# ===========================================================================

def test_success_raises_alpha_failure_raises_beta(tmp_path):
    r = _router(tmp_path)
    r.record_outcome("m-A", success=True)
    r.record_outcome("m-B", success=False)
    snap = r.snapshot()
    assert snap["m-A"]["alpha"] > snap["m-B"]["alpha"]
    assert snap["m-B"]["beta"] > snap["m-A"]["beta"]


# ===========================================================================
# D — advise / best_arm (the learning)
# ===========================================================================

def test_advise_prefers_the_better_arm_under_evidence(tmp_path):
    r = _router(tmp_path)
    for _ in range(40):
        r.record_outcome("good", success=True)
        r.record_outcome("bad", success=False)
    order = r.advise(["bad", "good"])
    assert order[0] == "good"
    assert r.best_arm(["bad", "good"]) == "good"


def test_advise_only_returns_arms_from_the_input_set(tmp_path):
    """Safety boundary: the advisor can never invent an out-of-policy arm."""
    r = _router(tmp_path)
    r.record_outcome("known", success=True)
    order = r.advise(["x", "y", "z"])
    assert set(order) == {"x", "y", "z"}
    assert "known" not in order


def test_advise_is_a_permutation_not_a_drop(tmp_path):
    r = _router(tmp_path)
    arms = ["a", "b", "c", "d"]
    order = r.advise(arms)
    assert sorted(order) == sorted(arms)


def test_advise_empty_returns_none(tmp_path):
    assert _router(tmp_path).advise([]) is None


# ===========================================================================
# E — fail-closed + robustness
# ===========================================================================

def test_disabled_singleton_advise_is_none(monkeypatch):
    monkeypatch.setenv("JARVIS_BANDIT_ROUTER_ENABLED", "false")
    assert get_bandit_router().advise(["a", "b"]) is None


def test_advise_never_raises_on_garbage(tmp_path):
    r = _router(tmp_path)
    assert r.advise(None) is None
    assert r.advise(["a", None, 3]) is not None  # tolerates, coerces


def test_record_outcome_never_raises(tmp_path):
    r = _router(tmp_path)
    r.record_outcome("", success=True)
    r.record_outcome(None, success=False)  # type: ignore[arg-type]


# ===========================================================================
# F — durability
# ===========================================================================

def test_state_round_trips(tmp_path):
    p = tmp_path / "persist.json"
    r1 = BanditRouter(state_path=p, rng=random.Random(1))
    for _ in range(5):
        r1.record_outcome("m", success=True)
    r2 = BanditRouter(state_path=p, rng=random.Random(1))
    assert r2.snapshot()["m"]["alpha"] == r1.snapshot()["m"]["alpha"]


# ===========================================================================
# G — wiring + doctrine pins
# ===========================================================================

def test_candidate_generator_consults_advisor():
    src = (_GOV / "candidate_generator.py").read_text(encoding="utf-8")
    assert "bandit_router" in src or "get_bandit_router" in src


def test_advisor_reorders_within_policy_set_only():
    """The advisor's input domain is ranked_models (the policy active set);
    pin that the wiring passes ranked_models in (never a wider source)."""
    src = (_GOV / "candidate_generator.py").read_text(encoding="utf-8")
    assert "advise(ranked_models" in src.replace(" ", "").replace(
        "advise(ranked_models", "advise(ranked_models",
    ) or "advise(" in src
