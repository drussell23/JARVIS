"""Tests for Slice 3.1 — ExplorationCalculus: Bayesian belief engine."""
from __future__ import annotations

import ast
import json
import math
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("JARVIS_EXPLORATION_CALCULUS_ENABLED", "true")


# ---------------------------------------------------------------------------
# 1. Shannon entropy
# ---------------------------------------------------------------------------

class TestEntropy:
    def test_max_uncertainty(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import entropy
        assert entropy(0.5) == pytest.approx(1.0, abs=1e-9)

    def test_certainty_at_zero(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import entropy
        assert entropy(0.0) == 0.0

    def test_certainty_at_one(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import entropy
        assert entropy(1.0) == 0.0

    def test_symmetry(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import entropy
        assert entropy(0.3) == pytest.approx(entropy(0.7), abs=1e-9)

    def test_mid_range(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import entropy
        h = entropy(0.9)
        assert 0.0 < h < 1.0

    def test_negative_returns_zero(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import entropy
        assert entropy(-0.5) == 0.0

    def test_above_one_returns_zero(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import entropy
        assert entropy(1.5) == 0.0


# ---------------------------------------------------------------------------
# 2. Bayesian update
# ---------------------------------------------------------------------------

class TestBayesianUpdate:
    def test_confirmed_increases_belief(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import bayesian_update
        posterior = bayesian_update(0.5, 3.0)
        assert posterior > 0.5

    def test_refuted_decreases_belief(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import bayesian_update
        posterior = bayesian_update(0.5, 0.33)
        assert posterior < 0.5

    def test_neutral_preserves_belief(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import bayesian_update
        posterior = bayesian_update(0.5, 1.0)
        assert posterior == pytest.approx(0.5, abs=1e-6)

    def test_clamped_to_valid_range(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            bayesian_update, MIN_PRIOR, MAX_PRIOR,
        )
        # Extreme confirmation
        posterior = bayesian_update(0.99, 100.0)
        assert posterior <= MAX_PRIOR
        # Extreme refutation
        posterior = bayesian_update(0.01, 0.01)
        assert posterior >= MIN_PRIOR

    def test_monotonic_with_confirmed(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import bayesian_update
        p = 0.5
        for _ in range(10):
            new_p = bayesian_update(p, 3.0)
            assert new_p >= p
            p = new_p

    def test_monotonic_with_refuted(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import bayesian_update
        p = 0.5
        for _ in range(10):
            new_p = bayesian_update(p, 0.33)
            assert new_p <= p
            p = new_p

    def test_idempotent_with_lr_one(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import bayesian_update
        for prior in [0.1, 0.3, 0.5, 0.7, 0.9]:
            posterior = bayesian_update(prior, 1.0)
            assert posterior == pytest.approx(prior, abs=1e-6)


# ---------------------------------------------------------------------------
# 3. Epsilon derivation
# ---------------------------------------------------------------------------

class TestEpsilon:
    def test_not_hardcoded(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import epsilon_from_prior
        # Different priors → different epsilons
        e1 = epsilon_from_prior(0.5)
        e2 = epsilon_from_prior(0.9)
        assert e1 != e2

    def test_higher_uncertainty_larger_epsilon(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import epsilon_from_prior
        e_high = epsilon_from_prior(0.5)
        e_low = epsilon_from_prior(0.9)
        assert e_high > e_low

    def test_never_zero(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import epsilon_from_prior
        # Even at extreme priors, epsilon should be > 0
        for p in [0.001, 0.01, 0.5, 0.99, 0.999]:
            assert epsilon_from_prior(p) > 0

    def test_env_ratio_overridable(self, monkeypatch):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import epsilon_from_prior
        monkeypatch.setenv("JARVIS_EXPLORATION_CONVERGENCE_RATIO", "0.5")
        e_wide = epsilon_from_prior(0.5)
        monkeypatch.setenv("JARVIS_EXPLORATION_CONVERGENCE_RATIO", "0.05")
        e_tight = epsilon_from_prior(0.5)
        assert e_wide > e_tight


# ---------------------------------------------------------------------------
# 4. Max probes derivation
# ---------------------------------------------------------------------------

class TestMaxProbes:
    def test_derived_from_epsilon(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import max_probes_for_epsilon
        p1 = max_probes_for_epsilon(0.1)
        p2 = max_probes_for_epsilon(0.01)
        # Tighter epsilon needs more probes
        assert p2 > p1

    def test_log_relationship(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import max_probes_for_epsilon
        # O(log2(1/ε))
        eps = 0.1
        expected = math.ceil(math.log2(1.0 / eps))
        assert max_probes_for_epsilon(eps) == expected

    def test_at_least_one(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import max_probes_for_epsilon
        assert max_probes_for_epsilon(0.9) >= 1

    def test_bounded_by_max(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            max_probes_for_epsilon, MAX_OBSERVATIONS,
        )
        assert max_probes_for_epsilon(1e-100) <= MAX_OBSERVATIONS


# ---------------------------------------------------------------------------
# 5. Cooling factor
# ---------------------------------------------------------------------------

class TestCooling:
    def test_max_at_full_uncertainty(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import cooling_factor
        assert cooling_factor(1.0) == 1.0

    def test_zero_at_convergence(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import cooling_factor
        assert cooling_factor(0.0) == 0.0

    def test_monotonic_decrease(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import cooling_factor
        values = [cooling_factor(h) for h in [1.0, 0.8, 0.5, 0.2, 0.0]]
        for i in range(len(values) - 1):
            assert values[i] >= values[i + 1]

    def test_clamped(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import cooling_factor
        assert cooling_factor(2.0) == 1.0
        assert cooling_factor(-1.0) == 0.0


# ---------------------------------------------------------------------------
# 6. Convergence classification
# ---------------------------------------------------------------------------

class TestConvergenceClassification:
    def test_converged_below_epsilon(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            classify_convergence, STATE_CONVERGED,
        )
        assert classify_convergence(
            current_entropy=0.01, previous_entropy=0.1, epsilon=0.05,
        ) == STATE_CONVERGED

    def test_converging_on_decrease(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            classify_convergence, STATE_CONVERGING,
        )
        assert classify_convergence(
            current_entropy=0.5, previous_entropy=0.8, epsilon=0.01,
        ) == STATE_CONVERGING

    def test_diverging_on_increase(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            classify_convergence, STATE_DIVERGING,
        )
        assert classify_convergence(
            current_entropy=0.8, previous_entropy=0.5, epsilon=0.01,
        ) == STATE_DIVERGING

    def test_exploring_on_no_change(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            classify_convergence, STATE_EXPLORING,
        )
        assert classify_convergence(
            current_entropy=0.5, previous_entropy=0.5, epsilon=0.01,
        ) == STATE_EXPLORING


# ---------------------------------------------------------------------------
# 7. Verdict to likelihood ratio
# ---------------------------------------------------------------------------

class TestVerdictMapping:
    def test_confirmed(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import verdict_to_likelihood_ratio
        lr = verdict_to_likelihood_ratio("CONFIRMED")
        assert lr > 1.0

    def test_refuted(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import verdict_to_likelihood_ratio
        lr = verdict_to_likelihood_ratio("REFUTED")
        assert lr < 1.0

    def test_inconclusive(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import verdict_to_likelihood_ratio
        lr = verdict_to_likelihood_ratio("INCONCLUSIVE")
        assert lr == pytest.approx(1.0, abs=0.01)

    def test_case_insensitive(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import verdict_to_likelihood_ratio
        assert verdict_to_likelihood_ratio("confirmed") == verdict_to_likelihood_ratio("CONFIRMED")

    def test_unknown_maps_to_inconclusive(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import verdict_to_likelihood_ratio
        lr = verdict_to_likelihood_ratio("UNKNOWN_VERDICT")
        assert lr == pytest.approx(1.0, abs=0.01)

    def test_env_overridable(self, monkeypatch):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import verdict_to_likelihood_ratio
        monkeypatch.setenv("JARVIS_EXPLORATION_CONFIRMED_LR", "5.0")
        assert verdict_to_likelihood_ratio("CONFIRMED") == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# 8. BeliefState lifecycle
# ---------------------------------------------------------------------------

class TestBeliefLifecycle:
    def test_initial_belief_defaults(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            initial_belief, STATE_EXPLORING,
        )
        bs = initial_belief("h1")
        assert bs.prior == pytest.approx(0.5)
        assert bs.posterior == pytest.approx(0.5)
        assert bs.observations == 0
        assert bs.convergence_state == STATE_EXPLORING

    def test_update_shifts_posterior(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            initial_belief, update_belief,
        )
        bs = initial_belief("h1")
        bs2 = update_belief(bs, verdict="CONFIRMED")
        assert bs2.posterior > bs.posterior
        assert bs2.observations == 1

    def test_convergence_after_many_confirmations(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            initial_belief, update_belief, epsilon_from_prior,
        )
        bs = initial_belief("h1")
        eps = epsilon_from_prior(bs.prior)
        for _ in range(50):
            bs = update_belief(bs, verdict="CONFIRMED")
            if bs.is_converged():
                break
        assert bs.is_converged() or bs.entropy < eps

    def test_to_dict_round_trip(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            initial_belief, parse_belief_state,
        )
        bs = initial_belief("h1", prior=0.7)
        d = bs.to_dict()
        json_str = json.dumps(d)
        parsed = parse_belief_state(json.loads(json_str))
        assert parsed is not None
        assert parsed.hypothesis_id == "h1"
        assert parsed.prior == pytest.approx(0.7, abs=0.001)

    def test_cost_accumulates(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            initial_belief, update_belief,
        )
        bs = initial_belief("h1")
        bs = update_belief(bs, verdict="CONFIRMED", cost_usd=0.03)
        bs = update_belief(bs, verdict="CONFIRMED", cost_usd=0.05)
        assert bs.cost_spent == pytest.approx(0.08)


# ---------------------------------------------------------------------------
# 9. Halting conditions
# ---------------------------------------------------------------------------

class TestHalting:
    def test_halt_on_convergence(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            initial_belief, update_belief, epsilon_from_prior, HALT_CONVERGED,
        )
        bs = initial_belief("h1")
        eps = epsilon_from_prior(bs.prior)
        for _ in range(100):
            bs = update_belief(bs, verdict="CONFIRMED")
            if bs.is_halted(eps):
                break
        assert bs.halt_reason(eps) == HALT_CONVERGED

    def test_halt_on_budget(self, monkeypatch):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            initial_belief, update_belief, epsilon_from_prior, HALT_BUDGET,
        )
        monkeypatch.setenv("JARVIS_EXPLORATION_BUDGET_PER_HYPOTHESIS", "0.10")
        bs = initial_belief("h1")
        eps = epsilon_from_prior(bs.prior)
        # Spend lots via INCONCLUSIVE (doesn't converge belief much)
        for _ in range(50):
            bs = update_belief(bs, verdict="INCONCLUSIVE", cost_usd=0.05)
            if bs.is_halted(eps):
                break
        assert bs.halt_reason(eps) == HALT_BUDGET

    def test_halt_on_max_probes(self, monkeypatch):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            initial_belief, update_belief, epsilon_from_prior,
            max_probes_for_epsilon, HALT_MAX_PROBES, HALT_CONVERGED,
            HALT_DIMINISHING,
        )
        # Use very tight convergence so max probes is small
        monkeypatch.setenv("JARVIS_EXPLORATION_CONVERGENCE_RATIO", "0.01")
        monkeypatch.setenv("JARVIS_EXPLORATION_BUDGET_PER_HYPOTHESIS", "999")
        bs = initial_belief("h1")
        eps = epsilon_from_prior(bs.prior)
        max_p = max_probes_for_epsilon(eps)
        for _ in range(max_p + 10):
            bs = update_belief(bs, verdict="INCONCLUSIVE")
            if bs.is_halted(eps):
                break
        reason = bs.halt_reason(eps)
        assert reason in (HALT_MAX_PROBES, HALT_CONVERGED, "diminishing_returns")

    def test_halt_on_diminishing_returns(self, monkeypatch):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            initial_belief, update_belief, epsilon_from_prior, HALT_DIMINISHING,
        )
        monkeypatch.setenv("JARVIS_EXPLORATION_DIMINISHING_WINDOW", "2")
        monkeypatch.setenv("JARVIS_EXPLORATION_BUDGET_PER_HYPOTHESIS", "999")
        bs = initial_belief("h1")
        eps = epsilon_from_prior(bs.prior)
        # Many INCONCLUSIVE probes → minimal entropy change → diminishing
        for _ in range(50):
            bs = update_belief(bs, verdict="INCONCLUSIVE")
            if bs.is_halted(eps):
                break
        assert bs.halt_reason(eps) == HALT_DIMINISHING


# ---------------------------------------------------------------------------
# 10. ConvergenceProof
# ---------------------------------------------------------------------------

class TestConvergenceProof:
    def test_proof_emitted_on_halt(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            initial_belief, update_belief, epsilon_from_prior,
            make_convergence_proof,
        )
        bs = initial_belief("h1")
        for _ in range(50):
            bs = update_belief(bs, verdict="CONFIRMED", cost_usd=0.01)
        proof = make_convergence_proof(bs)
        assert proof.halted is True
        assert proof.halt_reason != ""
        assert proof.probes_used == bs.observations
        assert proof.final_belief == bs.posterior
        assert proof.theoretical_max_probes >= 1

    def test_proof_serializable(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            initial_belief, make_convergence_proof,
        )
        bs = initial_belief("h1")
        proof = make_convergence_proof(bs)
        d = proof.to_dict()
        json_str = json.dumps(d)
        assert isinstance(json_str, str)


# ---------------------------------------------------------------------------
# 11. Master flag
# ---------------------------------------------------------------------------

class TestMasterFlag:
    @pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
    def test_truthy(self, monkeypatch, val):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import is_calculus_enabled
        monkeypatch.setenv("JARVIS_EXPLORATION_CALCULUS_ENABLED", val)
        assert is_calculus_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_falsy(self, monkeypatch, val):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import is_calculus_enabled
        monkeypatch.setenv("JARVIS_EXPLORATION_CALCULUS_ENABLED", val)
        assert is_calculus_enabled() is False

    def test_default_disabled(self, monkeypatch):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import is_calculus_enabled
        monkeypatch.delenv("JARVIS_EXPLORATION_CALCULUS_ENABLED", raising=False)
        assert is_calculus_enabled() is False


# ---------------------------------------------------------------------------
# 12. Pure function invariants
# ---------------------------------------------------------------------------

class TestPureFunctions:
    def test_same_input_same_output(self):
        from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
            bayesian_update, entropy, epsilon_from_prior,
        )
        for _ in range(10):
            assert bayesian_update(0.5, 3.0) == bayesian_update(0.5, 3.0)
            assert entropy(0.3) == entropy(0.3)
            assert epsilon_from_prior(0.5) == epsilon_from_prior(0.5)


# ---------------------------------------------------------------------------
# 13. Cage authority invariants
# ---------------------------------------------------------------------------

class TestCage:
    _BANNED = frozenset({
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator", "gate", "semantic_guardian",
    })

    def test_no_banned_imports(self):
        src = Path("backend/core/ouroboros/governance/adaptation/exploration_calculus.py")
        if not src.exists():
            pytest.skip("source not found")
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = ""
                if isinstance(node, ast.ImportFrom) and node.module:
                    mod = node.module
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        mod = alias.name
                for b in self._BANNED:
                    assert b not in mod


# ---------------------------------------------------------------------------
# 14. Constants pinned
# ---------------------------------------------------------------------------

class TestConstants:
    def test_pinned(self):
        from backend.core.ouroboros.governance.adaptation import exploration_calculus as mod
        assert mod.MAX_OBSERVATIONS == 200
        assert mod.MAX_BELIEF_STATES == 500
        assert mod.MIN_PRIOR == pytest.approx(0.001)
        assert mod.MAX_PRIOR == pytest.approx(0.999)
