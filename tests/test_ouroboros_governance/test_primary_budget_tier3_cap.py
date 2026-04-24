"""Tier 3 Reflex tests — _PRIMARY_MAX_TIMEOUT_S hard cap.

Scope: Manifesto §5 Tier 3 enforcement added 2026-04-24 after F1 Slice 4 S3
(bt-2026-04-24-204029) exposed a 153s DW primary hold starving the Claude
fallback. Pins the aggressive circuit-breaker contract: no primary call
may consume more than `_PRIMARY_MAX_TIMEOUT_S` seconds, even when the
remaining session budget is large.

Contract pinned:

1. The cap binds when `remaining > _PRIMARY_MAX_TIMEOUT_S + fallback_reserve`.
2. When remaining is small (e.g., tight retry), the fallback-reserve
   invariant still dominates — primary gets `remaining - fb_reserve`.
3. When remaining is very small (<= fb_reserve), primary gets 0.
4. The cap preserves all prior invariants (fraction, reserve).
5. Default value is 30 seconds.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
    _FALLBACK_MIN_RESERVE_S,
    _PRIMARY_BUDGET_FRACTION,
    _PRIMARY_MAX_TIMEOUT_S,
)


# ---------------------------------------------------------------------------
# (1) Module-level default
# ---------------------------------------------------------------------------


def test_primary_max_timeout_default_is_30s():
    """Default hard cap is 30 seconds per Manifesto §5 Tier 3 calibration."""
    # Note: this reads the module constant captured at import time. Env
    # overrides are tested below via re-import.
    assert _PRIMARY_MAX_TIMEOUT_S == 30.0


def test_fallback_min_reserve_default_raised_to_30s():
    """_FALLBACK_MIN_RESERVE_S raised from 20 to 30 for isolation
    defense-in-depth alongside Tier 3 cap."""
    assert _FALLBACK_MIN_RESERVE_S == 30.0


# ---------------------------------------------------------------------------
# (2) Hard-cap binding behavior
# ---------------------------------------------------------------------------


def test_hard_cap_binds_when_remaining_is_large():
    """remaining=220s (F1 S3 scenario): prior logic would give ~143s,
    new hard cap limits to 30s."""
    budget = CandidateGenerator._compute_primary_budget(220.0)
    assert budget == _PRIMARY_MAX_TIMEOUT_S
    assert budget == 30.0


def test_hard_cap_binds_at_60s_remaining():
    """At remaining=60s, fraction gives 39s, reserve gives 30s (60-30),
    cap gives 30s. Cap binds (tied with reserve)."""
    budget = CandidateGenerator._compute_primary_budget(60.0)
    assert budget == 30.0


def test_hard_cap_does_not_bind_at_smaller_budgets():
    """At remaining=40s, fraction gives 26s, reserve gives 14s (40-26.0),
    cap gives 30s. Fraction and reserve-via-percent dominate."""
    # fraction: 40 * 0.65 = 26.0
    # fb_reserve: min(30, 40 * 0.35) = min(30, 14) = 14
    # total_s - fb_reserve: 40 - 14 = 26
    # cap: 30
    # min(26, 26, 30) = 26
    budget = CandidateGenerator._compute_primary_budget(40.0)
    assert budget == pytest.approx(26.0)


# ---------------------------------------------------------------------------
# (3) Pre-existing invariants preserved
# ---------------------------------------------------------------------------


def test_zero_remaining_returns_zero():
    assert CandidateGenerator._compute_primary_budget(0.0) == 0.0


def test_negative_remaining_returns_zero():
    assert CandidateGenerator._compute_primary_budget(-10.0) == 0.0


def test_small_remaining_respects_fallback_reserve():
    """When total_s <= reserve, fb gets priority over primary."""
    # total=10, reserve=min(30, 10*0.35)=3.5, so primary max is min(6.5, 6.5, 30) = 6.5
    budget = CandidateGenerator._compute_primary_budget(10.0)
    assert budget == pytest.approx(6.5)
    # fb got 10 - 6.5 = 3.5s, which respects the scaled-down reserve
    assert (10.0 - budget) == pytest.approx(3.5)


def test_fraction_respected_when_smallest():
    """When the fraction is the smallest constraint (moderate remaining),
    fraction dominates over cap."""
    # total=45, fraction=29.25, reserve=min(30, 45*0.35)=15.75, budget=min(29.25, 29.25, 30) = 29.25
    budget = CandidateGenerator._compute_primary_budget(45.0)
    assert budget == pytest.approx(29.25)
    # fallback gets 45 - 29.25 = 15.75s, which is >= scaled reserve


# ---------------------------------------------------------------------------
# (4) Env override — documented via source-grep, not via importlib.reload
#     (reload-based env testing breaks downstream module-enum identity
#     invariants in test_candidate_generator.py's FailureMode / FailbackState
#     assertions — the `is` comparisons rely on singleton enum instances
#     that reload discards. The env knob is confirmed by source inspection
#     + the default-value test above; live verification via JARVIS_* env
#     at battle-test launch is the canonical integration path.)
# ---------------------------------------------------------------------------


def test_env_knob_is_read_from_correct_env_var():
    """Source-level verification that _PRIMARY_MAX_TIMEOUT_S reads
    OUROBOROS_PRIMARY_MAX_TIMEOUT_S (naming consistency with the other
    OUROBOROS_* provider cascade knobs: BUDGET_FRACTION, MIN_RESERVE_S)."""
    import inspect
    from backend.core.ouroboros.governance import candidate_generator as cg
    src = inspect.getsource(cg)
    assert 'os.environ.get("OUROBOROS_PRIMARY_MAX_TIMEOUT_S"' in src, (
        "_PRIMARY_MAX_TIMEOUT_S must read from OUROBOROS_PRIMARY_MAX_TIMEOUT_S "
        "env var (naming parallel to BUDGET_FRACTION + MIN_RESERVE_S)"
    )


# ---------------------------------------------------------------------------
# (5) Manifesto §5 quote — module docstring contains the binding rationale
# ---------------------------------------------------------------------------


def test_manifesto_rationale_documented():
    """The _PRIMARY_MAX_TIMEOUT_S constant definition must reference
    Manifesto §5 Tier 3 so future readers understand why the cap exists.
    Bit-rot guard: if someone deletes the comment block, this fails."""
    import inspect
    from backend.core.ouroboros.governance import candidate_generator as cg
    src = inspect.getsource(cg)
    # Look for key phrases from the Manifesto and from the S3 RCA
    assert "Tier 3 Reflex" in src
    assert "Manifesto §5" in src
    assert "_PRIMARY_MAX_TIMEOUT_S" in src
    assert "bt-2026-04-24-204029" in src  # S3 RCA anchor
