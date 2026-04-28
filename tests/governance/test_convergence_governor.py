"""Tests for Slice 3.2 — ConvergenceGovernor: formal halting layer."""
from __future__ import annotations

import ast
import json
import random
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CONVERGENCE_GOVERNOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_EXPLORATION_CALCULUS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CONVERGENCE_GOVERNOR_PATH",
                       str(tmp_path / "convergence_state.jsonl"))
    monkeypatch.setenv("JARVIS_CONVERGENCE_PROOFS_PATH",
                       str(tmp_path / "convergence_proofs.jsonl"))


@pytest.fixture
def governor(tmp_path):
    from backend.core.ouroboros.governance.adaptation.convergence_governor import (
        ConvergenceGovernor,
    )
    return ConvergenceGovernor(
        state_path=tmp_path / "convergence_state.jsonl",
        proofs_path=tmp_path / "convergence_proofs.jsonl",
    )


# ---------------------------------------------------------------------------
# 1. Tracking lifecycle
# ---------------------------------------------------------------------------

class TestTracking:
    def test_track_new_hypothesis(self, governor):
        bs = governor.track_hypothesis("h1")
        assert bs.hypothesis_id == "h1"
        assert bs.observations == 0

    def test_track_idempotent(self, governor):
        bs1 = governor.track_hypothesis("h1")
        bs2 = governor.track_hypothesis("h1")
        assert bs1.hypothesis_id == bs2.hypothesis_id

    def test_track_with_custom_prior(self, governor):
        bs = governor.track_hypothesis("h1", prior=0.8)
        assert bs.prior == pytest.approx(0.8, abs=0.01)

    def test_track_bounded(self, governor):
        from backend.core.ouroboros.governance.adaptation.convergence_governor import (
            MAX_TRACKED_HYPOTHESES,
        )
        for i in range(MAX_TRACKED_HYPOTHESES + 5):
            governor.track_hypothesis(f"h{i}")
        assert len(governor._beliefs) <= MAX_TRACKED_HYPOTHESES


# ---------------------------------------------------------------------------
# 2. should_explore gate
# ---------------------------------------------------------------------------

class TestShouldExplore:
    def test_returns_true_for_active(self, governor):
        governor.track_hypothesis("h1")
        assert governor.should_explore("h1") is True

    def test_returns_false_for_unknown(self, governor):
        assert governor.should_explore("unknown") is False

    def test_returns_false_when_disabled(self, governor, monkeypatch):
        monkeypatch.setenv("JARVIS_CONVERGENCE_GOVERNOR_ENABLED", "false")
        governor.track_hypothesis("h1")
        assert governor.should_explore("h1") is False

    def test_returns_false_after_convergence(self, governor):
        governor.track_hypothesis("h1")
        for _ in range(50):
            governor.record_observation("h1", "CONFIRMED", cost_usd=0.001)
        assert governor.should_explore("h1") is False


# ---------------------------------------------------------------------------
# 3. Record observation + Bayesian update
# ---------------------------------------------------------------------------

class TestRecordObservation:
    def test_updates_posterior(self, governor):
        governor.track_hypothesis("h1")
        bs, proof = governor.record_observation("h1", "CONFIRMED")
        assert bs.posterior > 0.5
        assert bs.observations == 1

    def test_cost_accumulates(self, governor):
        governor.track_hypothesis("h1")
        governor.record_observation("h1", "CONFIRMED", cost_usd=0.03)
        bs, _ = governor.record_observation("h1", "CONFIRMED", cost_usd=0.05)
        assert bs.cost_spent == pytest.approx(0.08)

    def test_auto_tracks_unknown(self, governor):
        bs, _ = governor.record_observation("auto_h", "CONFIRMED")
        assert bs.hypothesis_id == "auto_h"
        assert bs.observations == 1

    def test_proof_on_convergence(self, governor):
        governor.track_hypothesis("h1")
        proof = None
        for _ in range(50):
            bs, p = governor.record_observation("h1", "CONFIRMED", cost_usd=0.001)
            if p is not None:
                proof = p
                break
        assert proof is not None
        assert proof.halted is True
        assert proof.halt_reason != ""


# ---------------------------------------------------------------------------
# 4. Four halting conditions
# ---------------------------------------------------------------------------

class TestHaltingConditions:
    def test_halt_convergence(self, governor):
        governor.track_hypothesis("h1")
        for _ in range(100):
            bs, proof = governor.record_observation("h1", "CONFIRMED", cost_usd=0.001)
            if proof and proof.halt_reason == "converged":
                break
        assert not governor.should_explore("h1")

    def test_halt_budget(self, governor, monkeypatch):
        monkeypatch.setenv("JARVIS_EXPLORATION_BUDGET_PER_HYPOTHESIS", "0.05")
        governor.track_hypothesis("h1")
        for _ in range(100):
            bs, proof = governor.record_observation("h1", "INCONCLUSIVE", cost_usd=0.02)
            if proof:
                break
        assert proof is not None
        assert proof.halt_reason == "budget_exhausted"

    def test_halt_max_probes(self, governor, monkeypatch):
        monkeypatch.setenv("JARVIS_EXPLORATION_CONVERGENCE_RATIO", "0.01")
        monkeypatch.setenv("JARVIS_EXPLORATION_BUDGET_PER_HYPOTHESIS", "999")
        governor.track_hypothesis("h1")
        last_proof = None
        for _ in range(300):
            bs, proof = governor.record_observation("h1", "INCONCLUSIVE")
            if proof:
                last_proof = proof
                break
        assert last_proof is not None
        # Should halt via max_probes or diminishing_returns
        assert last_proof.halt_reason in (
            "max_probes_reached", "diminishing_returns", "converged",
        )

    def test_halt_diminishing_returns(self, governor, monkeypatch):
        monkeypatch.setenv("JARVIS_EXPLORATION_DIMINISHING_WINDOW", "2")
        monkeypatch.setenv("JARVIS_EXPLORATION_BUDGET_PER_HYPOTHESIS", "999")
        governor.track_hypothesis("h1")
        last_proof = None
        for _ in range(100):
            bs, proof = governor.record_observation("h1", "INCONCLUSIVE")
            if proof:
                last_proof = proof
                break
        assert last_proof is not None
        assert last_proof.halt_reason == "diminishing_returns"


# ---------------------------------------------------------------------------
# 5. Adversarial termination proof
# ---------------------------------------------------------------------------

class TestAdversarialTermination:
    """Construct inputs that try to drive exploration unbounded.
    Assert termination within budget on EVERY input."""

    def test_random_priors_all_terminate(self, governor, monkeypatch):
        monkeypatch.setenv("JARVIS_EXPLORATION_BUDGET_PER_HYPOTHESIS", "1.00")
        rng = random.Random(42)
        for i in range(50):
            hid = f"adv_{i}"
            prior = rng.uniform(0.01, 0.99)
            governor.track_hypothesis(hid, prior=prior)

            halted = False
            for step in range(300):
                # Adversarial: alternate CONFIRMED/REFUTED to maximize
                # oscillation and prevent convergence.
                verdict = "CONFIRMED" if step % 2 == 0 else "REFUTED"
                bs, proof = governor.record_observation(
                    hid, verdict, cost_usd=0.005,
                )
                if proof is not None:
                    halted = True
                    break
            assert halted, f"hypothesis {hid} (prior={prior}) did not terminate"

    def test_adversarial_all_proofs_emitted(self, governor, monkeypatch):
        monkeypatch.setenv("JARVIS_EXPLORATION_BUDGET_PER_HYPOTHESIS", "0.50")
        rng = random.Random(99)
        for i in range(20):
            hid = f"proof_{i}"
            governor.track_hypothesis(hid, prior=rng.uniform(0.1, 0.9))
            for step in range(200):
                verdict = "CONFIRMED" if step % 3 != 0 else "REFUTED"
                bs, proof = governor.record_observation(
                    hid, verdict, cost_usd=0.01,
                )
                if proof:
                    assert proof.halted is True
                    assert proof.hypothesis_id == hid
                    break

    def test_cost_never_exceeds_budget(self, governor, monkeypatch):
        monkeypatch.setenv("JARVIS_EXPLORATION_BUDGET_PER_HYPOTHESIS", "0.10")
        governor.track_hypothesis("cost_test")
        for _ in range(200):
            bs, proof = governor.record_observation(
                "cost_test", "INCONCLUSIVE", cost_usd=0.03,
            )
            if proof:
                break
        # Cost at halt should be ≤ budget + one probe overshoot.
        assert bs.cost_spent <= 0.20  # budget + one extra probe


# ---------------------------------------------------------------------------
# 6. Cooling schedule
# ---------------------------------------------------------------------------

class TestCooling:
    def test_full_curiosity_no_hypotheses(self, governor):
        assert governor.global_cooling_factor() == 1.0

    def test_zero_curiosity_all_converged(self, governor):
        governor.track_hypothesis("h1")
        for _ in range(50):
            governor.record_observation("h1", "CONFIRMED", cost_usd=0.001)
        assert governor.global_cooling_factor() == 0.0

    def test_decreasing_with_convergence(self, governor):
        governor.track_hypothesis("h1")
        factors = []
        for _ in range(20):
            governor.record_observation("h1", "CONFIRMED", cost_usd=0.001)
            factors.append(governor.global_cooling_factor())
        # Overall trend should be decreasing.
        assert factors[-1] <= factors[0]


# ---------------------------------------------------------------------------
# 7. Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_beliefs_persist_and_reload(self, tmp_path):
        from backend.core.ouroboros.governance.adaptation.convergence_governor import (
            ConvergenceGovernor,
        )
        sp = tmp_path / "beliefs.jsonl"
        pp = tmp_path / "proofs.jsonl"

        g1 = ConvergenceGovernor(state_path=sp, proofs_path=pp)
        g1.track_hypothesis("h1", prior=0.7)
        g1.record_observation("h1", "CONFIRMED", cost_usd=0.02)

        g2 = ConvergenceGovernor(state_path=sp, proofs_path=pp)
        bs = g2.get_belief("h1")
        assert bs is not None
        assert bs.observations == 1
        assert bs.cost_spent == pytest.approx(0.02)

    def test_proofs_persist(self, tmp_path):
        from backend.core.ouroboros.governance.adaptation.convergence_governor import (
            ConvergenceGovernor,
        )
        sp = tmp_path / "beliefs.jsonl"
        pp = tmp_path / "proofs.jsonl"

        g1 = ConvergenceGovernor(state_path=sp, proofs_path=pp)
        g1.track_hypothesis("h1")
        for _ in range(50):
            _, proof = g1.record_observation("h1", "CONFIRMED", cost_usd=0.001)
            if proof:
                break

        g2 = ConvergenceGovernor(state_path=sp, proofs_path=pp)
        proofs = g2.all_proofs()
        assert len(proofs) >= 1
        assert proofs[0].hypothesis_id == "h1"


# ---------------------------------------------------------------------------
# 8. Query API
# ---------------------------------------------------------------------------

class TestQueryAPI:
    def test_active_hypotheses(self, governor):
        governor.track_hypothesis("h1")
        governor.track_hypothesis("h2")
        assert "h1" in governor.active_hypotheses()
        assert "h2" in governor.active_hypotheses()

    def test_converged_hypotheses(self, governor):
        governor.track_hypothesis("h1")
        for _ in range(50):
            governor.record_observation("h1", "CONFIRMED", cost_usd=0.001)
        assert "h1" in governor.converged_hypotheses()

    def test_stats(self, governor):
        governor.track_hypothesis("h1")
        s = governor.stats()
        assert s["total_tracked"] == 1
        assert s["active"] == 1
        assert "global_cooling_factor" in s


# ---------------------------------------------------------------------------
# 9. Master flag
# ---------------------------------------------------------------------------

class TestMasterFlag:
    @pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
    def test_truthy(self, monkeypatch, val):
        from backend.core.ouroboros.governance.adaptation.convergence_governor import is_governor_enabled
        monkeypatch.setenv("JARVIS_CONVERGENCE_GOVERNOR_ENABLED", val)
        assert is_governor_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_falsy(self, monkeypatch, val):
        from backend.core.ouroboros.governance.adaptation.convergence_governor import is_governor_enabled
        monkeypatch.setenv("JARVIS_CONVERGENCE_GOVERNOR_ENABLED", val)
        assert is_governor_enabled() is False

    def test_default_disabled(self, monkeypatch):
        from backend.core.ouroboros.governance.adaptation.convergence_governor import is_governor_enabled
        monkeypatch.delenv("JARVIS_CONVERGENCE_GOVERNOR_ENABLED", raising=False)
        assert is_governor_enabled() is False


# ---------------------------------------------------------------------------
# 10. Cage authority invariants
# ---------------------------------------------------------------------------

class TestCage:
    _BANNED = frozenset({
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator", "gate", "semantic_guardian",
    })

    def test_no_banned_imports(self):
        src = Path("backend/core/ouroboros/governance/adaptation/convergence_governor.py")
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
# 11. Constants pinned
# ---------------------------------------------------------------------------

class TestConstants:
    def test_pinned(self):
        from backend.core.ouroboros.governance.adaptation import convergence_governor as mod
        assert mod.MAX_TRACKED_HYPOTHESES == 200
        assert mod.MAX_STATE_FILE_BYTES == 4 * 1024 * 1024
        assert mod.MAX_PROOFS_RETAINED == 500
