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
    """The Tier 3 cap constants must reference Manifesto §5 Tier 3 so
    future readers understand why the cap exists. Bit-rot guard: if
    someone deletes the comment block, this fails."""
    import inspect
    from backend.core.ouroboros.governance import candidate_generator as cg
    src = inspect.getsource(cg)
    assert "Tier 3 Reflex" in src
    assert "Manifesto §5" in src
    assert "_PRIMARY_MAX_TIMEOUT_S" in src
    assert "_TIER3_REFLEX_HARD_CAP_S" in src
    assert "bt-2026-04-24-204029" in src  # S3 RCA anchor
    assert "bt-2026-04-24-213248" in src  # S4 RCA anchor (DW-as-Tier0-AND-primary)


# ---------------------------------------------------------------------------
# (6) Tier 0 hard cap — applies when DW is both Tier 0 and Primary
#     (the S4 configuration: J-Prime unhealthy → DW promoted to primary
#     but still tried first via Tier 0 fast path; _call_primary is
#     skipped, so the cap must be enforced in _compute_tier0_budget too).
# ---------------------------------------------------------------------------


def test_tier3_reflex_hard_cap_default_is_30s():
    """Shared cap source for both primary and Tier 0 call paths."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _TIER3_REFLEX_HARD_CAP_S,
    )
    assert _TIER3_REFLEX_HARD_CAP_S == 30.0


def test_tier0_budget_standard_route_binds_at_hard_cap():
    """STANDARD route at large remaining: hard cap binds instead of
    _TIER0_MAX_WAIT_S (90s). This is the S4 scenario directly."""
    # remaining=220s, standard route, trivial complexity
    # fraction: 220 * 0.65 * 1.0 = 143
    # max_wait: 90 (standard)
    # reserve: min(25, 220*0.35) = 25; 220 - 25 = 195
    # tier3 cap: 30
    # min(143, 90, 195, 30) = 30
    budget = CandidateGenerator._compute_tier0_budget(
        220.0, complexity="trivial", provider_route="standard",
    )
    assert budget == 30.0


def test_tier0_budget_s4_scenario_exact_inputs():
    """Reproduce the S4 DW-as-primary-and-tier0 scenario at exactly the
    observed inputs. Prior patch (_compute_primary_budget only) gave 143s
    via fraction. Post-patch, Tier 0 path also caps at 30s."""
    # S4 observed: route=standard, remaining_s=220, complexity=moderate
    # Without Tier 3 cap: min(220*0.65=143, 90, 220-25=195) = 90
    # With Tier 3 cap: min(143, 90, 195, 30) = 30
    budget = CandidateGenerator._compute_tier0_budget(
        220.0, complexity="moderate", provider_route="standard",
    )
    assert budget == 30.0, (
        f"S4 scenario must cap at 30s Tier 3 reflex; got {budget}"
    )


def test_tier0_budget_immediate_route_unchanged():
    """IMMEDIATE route skips DW entirely (returns 0) — cap is irrelevant."""
    budget = CandidateGenerator._compute_tier0_budget(
        220.0, complexity="trivial", provider_route="immediate",
    )
    assert budget == 0.0


def test_tier0_budget_background_route_skips_tier3_cap():
    """BACKGROUND route has its own early return min(total_s, 180.0)
    BEFORE the _compute_tier0_budget main logic. Tier 3 cap does NOT
    apply here because BG already has DW-only semantics (no Claude
    fallback to cascade TO). Test pins current behavior — if future
    work extends Tier 3 to BG, update this test."""
    budget = CandidateGenerator._compute_tier0_budget(
        220.0, complexity="trivial", provider_route="background",
    )
    # BG: min(220, 180) = 180
    assert budget == 180.0


def test_tier0_budget_speculative_route_skips_tier3_cap():
    """SPECULATIVE similarly uses its own early return min(total_s, 300).
    Tier 3 cap does not apply (fire-and-forget, no Claude cascade)."""
    budget = CandidateGenerator._compute_tier0_budget(
        220.0, complexity="trivial", provider_route="speculative",
    )
    # Speculative: min(220, 300) = 220
    assert budget == 220.0


def test_tier0_budget_complex_route_capped_at_tier3():
    """COMPLEX route overrides max_wait=120s but Tier 3 cap still binds
    at 30s. The COMPLEX 'DW executes plan' semantics are respected in
    the inner fraction/max_wait logic; the outer cap is the reflex
    ceiling. If operators legitimately need longer COMPLEX Tier 0 time,
    they can raise OUROBOROS_TIER3_REFLEX_HARD_CAP_S."""
    # COMPLEX + remaining=220 + trivial complexity
    # fraction: 220 * 0.65 * max(1.0, 1.231) = 220 * 0.8 = 176
    # max_wait: 120
    # reserve: min(20, 220*(1-0.8)) = min(20, 44) = 20; 220-20 = 200
    # tier3 cap: 30
    # min(176, 120, 200, 30) = 30
    budget = CandidateGenerator._compute_tier0_budget(
        220.0, complexity="trivial", provider_route="complex",
    )
    assert budget == 30.0


def test_tier0_budget_small_remaining_preserves_invariants():
    """When total_s is small, fraction/reserve dominate (not cap).
    Pre-existing behavior preserved for tight-budget cases.

    At remaining=40s, moderate complexity, standard route:
      fraction: 40 * 0.65 * 1.077 = 28.00  (multiplier from complexity table)
      max_wait: 90
      reserve: min(25, 40*(1-0.70)) = min(25, 12) = 12; 40 - 12 = 28
      tier3 cap: 30
      min(28, 90, 28, 30) = 28  — fraction/reserve-via-percent binds, not cap
    """
    budget = CandidateGenerator._compute_tier0_budget(
        40.0, complexity="moderate", provider_route="standard",
    )
    assert budget == pytest.approx(28.0, abs=0.05)


def test_tier0_budget_trivial_complexity_fraction_much_smaller_than_cap():
    """Trivial ops get a tight fraction (0.31 multiplier → ~20% of budget).
    At remaining=40, trivial: fraction ≈ 8s which is well below the 30s
    Tier 3 cap. The cap is not the binding constraint; fraction-first
    logic preserved."""
    budget = CandidateGenerator._compute_tier0_budget(
        40.0, complexity="trivial", provider_route="standard",
    )
    # fraction: 40 * 0.65 * 0.31 = 8.06
    assert budget == pytest.approx(8.06, abs=0.1)


def test_tier0_budget_zero_remaining_returns_zero():
    """Preserves pre-existing invariant."""
    assert CandidateGenerator._compute_tier0_budget(0.0) == 0.0
    assert CandidateGenerator._compute_tier0_budget(-5.0) == 0.0
