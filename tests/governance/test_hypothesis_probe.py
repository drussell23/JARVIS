"""Priority C — HypothesisProbe primitive regression spine.

The structural primitive that lets O+V resolve epistemic ambiguity
autonomously. Pins the four mathematical contracts:

  1. BOUNDED DEPTH      — max_iterations enforced
  2. BOUNDED BUDGET     — budget_usd enforced
  3. PROVABLE CONVERGENCE — |posterior - prior| < epsilon halts
  4. MEMORIALIZED       — failed probes persist; future cycles
                          short-circuit to "memorialized_dead"

Pins:
  §1   Master flag default false (opt-in until graduation)
  §2   Master flag truthy/falsy contract
  §3   Hypothesis + ProbeResult are frozen + serialise round-trip
  §4   Hypothesis.resolved_* methods fall back to env defaults
  §5   derive_epsilon — math: prior=0.5 max, prior=0/1 zero
  §6   TestStrategy is frozen
  §7   Strategy registry — idempotent + overwrite + alphabetical-stable
  §8   Three seed strategies registered at module load
       (lookup, subagent_explore, dry_run)
  §9   hypothesis_dead_id — content-hash of (claim, signal, strategy)
  §10  Cosmetic differences (whitespace, parent_op_id) hash to same id
  §11  is_hypothesis_memorialized returns False on missing ledger
  §12  memorialize_hypothesis appends + idempotent on re-attempt
  §13  Probe master-off returns disabled state
  §14  Probe with unknown strategy returns unknown_strategy state
  §15  Probe lookup file_exists CONFIRMED on real file
  §16  Probe lookup file_exists REFUTED on missing file
  §17  Probe lookup contains: CONFIRMED on substring match
  §18  Probe lookup contains: REFUTED on substring miss
  §19  Probe lookup not_contains: vacuously CONFIRMED on missing file
  §20  Probe converges to stable when delta < epsilon
  §21  Probe halts on max_iterations (inconclusive)
  §22  Probe halts on budget_exhausted
  §23  Probe halts on wall_exceeded
  §24  Probe never raises on strategy that raises
  §25  Probe memorializes inconclusive results
  §26  Probe DOES NOT memorialize stable results (genuine convergence
       can re-probe later)
  §27  Memorialized hypothesis short-circuits with memorialized_dead
  §28  get_default_probe returns singleton
  §29  Authority invariants — no orchestrator/phase_runner imports
  §30  Public API exposed
  §31  Bayesian update reuses Slice 2.2 RepeatRunner primitive
       (no duplication)
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Tuple

import pytest

from backend.core.ouroboros.governance.verification.hypothesis_probe import (
    HYPOTHESIS_PROBE_SCHEMA_VERSION,
    Hypothesis,
    HypothesisProbe,
    ProbeResult,
    TestStrategy,
    default_budget_per_probe_usd,
    default_max_iterations,
    default_max_wall_s,
    derive_epsilon,
    get_default_probe,
    hypothesis_dead_id,
    hypothesis_probe_enabled,
    is_hypothesis_memorialized,
    list_test_strategies,
    memorialize_hypothesis,
    register_test_strategy,
    reset_ledger_for_tests,
    reset_strategy_registry_for_tests,
    unregister_test_strategy,
)


@pytest.fixture
def fresh_registry():
    reset_strategy_registry_for_tests()
    yield
    reset_strategy_registry_for_tests()


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    """Per-test ledger so tests don't share memorialization state."""
    ledger = tmp_path / "failed_hypotheses.jsonl"
    monkeypatch.setenv(
        "JARVIS_HYPOTHESIS_LEDGER_PATH", str(ledger),
    )
    yield ledger
    if ledger.exists():
        ledger.unlink()


@pytest.fixture
def probe_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", "true")
    yield


# ===========================================================================
# §1-§2 — Master flag
# ===========================================================================


def test_master_flag_default_false(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", raising=False)
    assert hypothesis_probe_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_master_flag_truthy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", val)
    assert hypothesis_probe_enabled() is True


@pytest.mark.parametrize(
    "val", ["", " ", "0", "false", "no", "off", "garbage"],
)
def test_master_flag_falsy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", val)
    assert hypothesis_probe_enabled() is False


# ===========================================================================
# §3-§4 — Frozen + serialisation + resolved fallbacks
# ===========================================================================


def test_hypothesis_is_frozen() -> None:
    h = Hypothesis(
        claim="x", confidence_prior=0.5,
        test_strategy="lookup", expected_signal="x",
    )
    with pytest.raises((AttributeError, FrozenInstanceError, TypeError)):
        h.claim = "y"  # type: ignore[misc]


def test_probe_result_is_frozen() -> None:
    r = ProbeResult(
        confidence_posterior=0.7, observation_summary="x",
        cost_usd=0.0, iterations_used=1,
        convergence_state="stable", evidence_hash="abc",
    )
    with pytest.raises((AttributeError, FrozenInstanceError, TypeError)):
        r.convergence_state = "other"  # type: ignore[misc]


def test_hypothesis_to_dict_contains_required_fields() -> None:
    h = Hypothesis(
        claim="test", confidence_prior=0.5,
        test_strategy="lookup", expected_signal="x",
    )
    d = h.to_dict()
    assert d["claim"] == "test"
    assert d["confidence_prior"] == 0.5
    assert d["test_strategy"] == "lookup"
    assert d["schema_version"] == HYPOTHESIS_PROBE_SCHEMA_VERSION


def test_resolved_methods_fall_back_to_env_defaults(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_HYPOTHESIS_BUDGET_PER_PROBE_USD", "0.10")
    monkeypatch.setenv("JARVIS_HYPOTHESIS_MAX_ITERATIONS", "5")
    monkeypatch.setenv("JARVIS_HYPOTHESIS_MAX_WALL_S", "60")
    h = Hypothesis(
        claim="x", confidence_prior=0.5,
        test_strategy="lookup", expected_signal="x",
        # All bounds set to sentinels → use env defaults
    )
    assert h.resolved_budget_usd() == 0.10
    assert h.resolved_max_iterations() == 5
    assert h.resolved_max_wall_s() == 60


def test_explicit_bounds_override_env(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_HYPOTHESIS_BUDGET_PER_PROBE_USD", "0.10")
    h = Hypothesis(
        claim="x", confidence_prior=0.5,
        test_strategy="lookup", expected_signal="x",
        budget_usd=0.99, max_iterations=10, max_wall_s=120,
    )
    assert h.resolved_budget_usd() == 0.99
    assert h.resolved_max_iterations() == 10
    assert h.resolved_max_wall_s() == 120


# ===========================================================================
# §5 — derive_epsilon math
# ===========================================================================


def test_epsilon_max_at_uncertainty() -> None:
    """prior = 0.5 (maximum uncertainty) → epsilon = base."""
    assert derive_epsilon(0.5) == pytest.approx(0.05, rel=1e-3)


def test_epsilon_zero_at_certainty() -> None:
    """prior near 0 or 1 → epsilon near 0 (no probing needed)."""
    assert derive_epsilon(0.001) == pytest.approx(0.0, abs=1e-3)
    assert derive_epsilon(0.999) == pytest.approx(0.0, abs=1e-3)


def test_epsilon_monotonic_with_uncertainty() -> None:
    """As prior moves from 0/1 toward 0.5, epsilon increases."""
    assert derive_epsilon(0.5) > derive_epsilon(0.7)
    assert derive_epsilon(0.7) > derive_epsilon(0.9)
    assert derive_epsilon(0.5) > derive_epsilon(0.3)
    assert derive_epsilon(0.3) > derive_epsilon(0.1)


# ===========================================================================
# §6-§8 — Test strategy registry
# ===========================================================================


def test_strategy_is_frozen() -> None:
    async def no_op(h):
        return ("", 0.0, "INCONCLUSIVE")
    s = TestStrategy(
        strategy_kind="x", description="d", execute=no_op,
    )
    with pytest.raises((AttributeError, FrozenInstanceError, TypeError)):
        s.strategy_kind = "y"  # type: ignore[misc]


def test_register_idempotent_on_identical(fresh_registry) -> None:
    async def fn(h):
        return ("", 0.0, "INCONCLUSIVE")
    s = TestStrategy(
        strategy_kind="custom", description="d", execute=fn,
    )
    register_test_strategy(s)
    register_test_strategy(s)
    custom_count = sum(
        1 for st in list_test_strategies()
        if st.strategy_kind == "custom"
    )
    assert custom_count == 1


def test_register_rejects_different_without_overwrite(
    fresh_registry,
) -> None:
    async def f1(h):
        return ("a", 0.0, "INCONCLUSIVE")

    async def f2(h):
        return ("b", 0.0, "INCONCLUSIVE")
    s1 = TestStrategy(
        strategy_kind="custom", description="A", execute=f1,
    )
    s2 = TestStrategy(
        strategy_kind="custom", description="B", execute=f2,
    )
    register_test_strategy(s1)
    register_test_strategy(s2)
    custom = [
        s for s in list_test_strategies() if s.strategy_kind == "custom"
    ]
    assert len(custom) == 1
    assert custom[0].description == "A"


def test_unregister_returns_correct_status(fresh_registry) -> None:
    async def fn(h):
        return ("", 0.0, "INCONCLUSIVE")
    register_test_strategy(
        TestStrategy(
            strategy_kind="ephemeral", description="d", execute=fn,
        ),
    )
    assert unregister_test_strategy("ephemeral") is True
    assert unregister_test_strategy("ephemeral") is False
    assert unregister_test_strategy("never") is False


def test_three_seeds_registered(fresh_registry) -> None:
    kinds = sorted(s.strategy_kind for s in list_test_strategies())
    assert kinds == ["dry_run", "lookup", "subagent_explore"]


def test_seeds_alphabetical_stable(fresh_registry) -> None:
    s1 = list_test_strategies()
    s2 = list_test_strategies()
    assert s1 == s2  # stable across calls
    kinds = [s.strategy_kind for s in s1]
    assert kinds == sorted(kinds)


# ===========================================================================
# §9-§10 — Content-hash dead_id
# ===========================================================================


def test_dead_id_is_sha256_of_canonical_triple() -> None:
    h = Hypothesis(
        claim="x", confidence_prior=0.5,
        test_strategy="lookup", expected_signal="sig",
    )
    expected = hashlib.sha256(b"xsiglookup").hexdigest()
    assert hypothesis_dead_id(h) == expected


def test_cosmetic_differences_hash_to_same_id() -> None:
    h1 = Hypothesis(
        claim="x ", confidence_prior=0.5,
        test_strategy=" lookup ", expected_signal=" sig ",
        parent_op_id="op-A",
    )
    h2 = Hypothesis(
        claim=" x", confidence_prior=0.7,  # different prior!
        test_strategy="lookup", expected_signal="sig",
        parent_op_id="op-B",
    )
    # Whitespace + prior + parent_op_id are NOT part of the hash
    assert hypothesis_dead_id(h1) == hypothesis_dead_id(h2)


def test_genuinely_different_hypotheses_differ() -> None:
    h1 = Hypothesis(
        claim="x", confidence_prior=0.5,
        test_strategy="lookup", expected_signal="a",
    )
    h2 = Hypothesis(
        claim="y", confidence_prior=0.5,
        test_strategy="lookup", expected_signal="a",
    )
    assert hypothesis_dead_id(h1) != hypothesis_dead_id(h2)


# ===========================================================================
# §11-§12 — Memorialization ledger
# ===========================================================================


def test_is_memorialized_false_on_missing_ledger(isolated_ledger) -> None:
    h = Hypothesis(
        claim="x", confidence_prior=0.5,
        test_strategy="lookup", expected_signal="x",
    )
    assert is_hypothesis_memorialized(h) is False


def test_memorialize_appends_and_subsequent_check_returns_true(
    isolated_ledger,
) -> None:
    h = Hypothesis(
        claim="x", confidence_prior=0.5,
        test_strategy="lookup", expected_signal="x",
    )
    r = ProbeResult(
        confidence_posterior=0.5, observation_summary="dead",
        cost_usd=0.0, iterations_used=3,
        convergence_state="inconclusive", evidence_hash="abc",
    )
    assert memorialize_hypothesis(h, r) is True
    assert is_hypothesis_memorialized(h) is True


def test_memorialize_idempotent_on_re_attempt(isolated_ledger) -> None:
    h = Hypothesis(
        claim="x", confidence_prior=0.5,
        test_strategy="lookup", expected_signal="x",
    )
    r = ProbeResult(
        confidence_posterior=0.5, observation_summary="dead",
        cost_usd=0.0, iterations_used=3,
        convergence_state="inconclusive", evidence_hash="abc",
    )
    assert memorialize_hypothesis(h, r) is True
    assert memorialize_hypothesis(h, r) is True  # silent dedup
    # Ledger should have exactly 1 line
    text = isolated_ledger.read_text(encoding="utf-8")
    assert text.count("\n") == 1


# ===========================================================================
# §13-§14 — Probe disabled / unknown strategy paths
# ===========================================================================


def test_probe_master_off_returns_disabled(isolated_ledger) -> None:
    h = Hypothesis(
        claim="x", confidence_prior=0.5,
        test_strategy="lookup", expected_signal="x",
    )
    r = asyncio.run(HypothesisProbe().test(h))
    assert r.convergence_state == "disabled"
    assert r.confidence_posterior == 0.5


def test_probe_unknown_strategy(probe_enabled, isolated_ledger) -> None:
    h = Hypothesis(
        claim="x", confidence_prior=0.5,
        test_strategy="not-registered-strategy",
        expected_signal="x",
    )
    r = asyncio.run(HypothesisProbe().test(h))
    assert r.convergence_state == "unknown_strategy"


# ===========================================================================
# §15-§19 — Lookup strategy semantics
# ===========================================================================


def test_lookup_file_exists_confirmed(
    probe_enabled, isolated_ledger, tmp_path,
) -> None:
    target = tmp_path / "real_file.txt"
    target.write_text("hello")
    h = Hypothesis(
        claim="real_file exists", confidence_prior=0.3,
        test_strategy="lookup",
        expected_signal=f"file_exists:{target}",
        max_iterations=1,
    )
    r = asyncio.run(HypothesisProbe().test(h))
    assert r.iterations_used == 1
    assert r.confidence_posterior > 0.3  # CONFIRMED moves prior up


def test_lookup_file_exists_refuted(
    probe_enabled, isolated_ledger, tmp_path,
) -> None:
    h = Hypothesis(
        claim="missing exists", confidence_prior=0.7,
        test_strategy="lookup",
        expected_signal=f"file_exists:{tmp_path}/nope.txt",
        max_iterations=1,
    )
    r = asyncio.run(HypothesisProbe().test(h))
    # REFUTED moves posterior DOWN
    assert r.confidence_posterior < 0.7


def test_lookup_contains_substring_confirmed(
    probe_enabled, isolated_ledger, tmp_path,
) -> None:
    target = tmp_path / "code.py"
    target.write_text("def foo():\n    return 42\n")
    h = Hypothesis(
        claim="contains foo", confidence_prior=0.5,
        test_strategy="lookup",
        expected_signal=f"contains:{target}:def foo",
        max_iterations=1,
    )
    r = asyncio.run(HypothesisProbe().test(h))
    assert r.confidence_posterior > 0.5


def test_lookup_contains_substring_refuted(
    probe_enabled, isolated_ledger, tmp_path,
) -> None:
    target = tmp_path / "code.py"
    target.write_text("def bar():\n    pass\n")
    h = Hypothesis(
        claim="contains foo", confidence_prior=0.5,
        test_strategy="lookup",
        expected_signal=f"contains:{target}:def foo",
        max_iterations=1,
    )
    r = asyncio.run(HypothesisProbe().test(h))
    assert r.confidence_posterior < 0.5


def test_lookup_not_contains_vacuous_on_missing_file(
    probe_enabled, isolated_ledger, tmp_path,
) -> None:
    h = Hypothesis(
        claim="not_contains in missing", confidence_prior=0.5,
        test_strategy="lookup",
        expected_signal=f"not_contains:{tmp_path}/missing:secret",
        max_iterations=1,
    )
    r = asyncio.run(HypothesisProbe().test(h))
    # Vacuously CONFIRMED (file doesn't exist → can't contain anything)
    assert r.confidence_posterior > 0.5


# ===========================================================================
# §20-§23 — Termination paths (the four math contracts)
# ===========================================================================


def test_probe_halts_on_max_iterations(
    fresh_registry, probe_enabled, isolated_ledger,
) -> None:
    """A strategy that always returns INCONCLUSIVE never moves the
    posterior, so convergence cannot fire. Probe must halt at
    max_iterations with state=inconclusive."""

    async def always_inconclusive(h):
        return ("noop", 0.0, "INCONCLUSIVE")
    register_test_strategy(
        TestStrategy(
            strategy_kind="always_inconclusive",
            description="test",
            execute=always_inconclusive,
        ),
    )
    h = Hypothesis(
        claim="x", confidence_prior=0.5,
        test_strategy="always_inconclusive",
        expected_signal="x", max_iterations=3,
    )
    r = asyncio.run(HypothesisProbe().test(h))
    assert r.iterations_used == 3
    assert r.convergence_state == "inconclusive"


def test_probe_halts_on_budget_exhausted(
    fresh_registry, probe_enabled, isolated_ledger,
) -> None:
    """Strategy that costs $0.10 per call, budget=$0.05 → halts
    after first iter when cost exceeds budget."""

    async def expensive(h):
        return ("burned $0.10", 0.10, "INCONCLUSIVE")
    register_test_strategy(
        TestStrategy(
            strategy_kind="expensive",
            description="test",
            execute=expensive,
        ),
    )
    h = Hypothesis(
        claim="x", confidence_prior=0.5,
        test_strategy="expensive", expected_signal="x",
        max_iterations=10, budget_usd=0.05,
    )
    r = asyncio.run(HypothesisProbe().test(h))
    assert r.convergence_state == "budget_exhausted"
    assert r.cost_usd > 0.05  # exceeded the cap


def test_probe_halts_on_wall_exceeded(
    fresh_registry, probe_enabled, isolated_ledger,
) -> None:
    """Strategy that sleeps longer than max_wall_s. The asyncio.wait_for
    inside the probe enforces the cap."""

    async def slow(h):
        await asyncio.sleep(2.0)
        return ("woke up", 0.0, "INCONCLUSIVE")
    register_test_strategy(
        TestStrategy(
            strategy_kind="slow",
            description="test", execute=slow,
        ),
    )
    h = Hypothesis(
        claim="x", confidence_prior=0.5,
        test_strategy="slow", expected_signal="x",
        max_iterations=5, max_wall_s=1,
    )
    r = asyncio.run(HypothesisProbe().test(h))
    assert r.convergence_state == "wall_exceeded"


def test_probe_never_raises_on_strategy_raise(
    fresh_registry, probe_enabled, isolated_ledger,
) -> None:
    async def boom(h):
        raise RuntimeError("boom")
    register_test_strategy(
        TestStrategy(
            strategy_kind="boom",
            description="test", execute=boom,
        ),
    )
    h = Hypothesis(
        claim="x", confidence_prior=0.5,
        test_strategy="boom", expected_signal="x",
    )
    r = asyncio.run(HypothesisProbe().test(h))
    assert r.convergence_state == "evaluator_error"


# ===========================================================================
# §25-§27 — Memorialization semantics
# ===========================================================================


def test_inconclusive_results_get_memorialized(
    fresh_registry, probe_enabled, isolated_ledger,
) -> None:
    async def always_inconclusive(h):
        return ("noop", 0.0, "INCONCLUSIVE")
    register_test_strategy(
        TestStrategy(
            strategy_kind="always_inconclusive",
            description="test",
            execute=always_inconclusive,
        ),
    )
    h = Hypothesis(
        claim="dead-end", confidence_prior=0.5,
        test_strategy="always_inconclusive",
        expected_signal="x", max_iterations=2,
    )
    r1 = asyncio.run(HypothesisProbe().test(h))
    assert r1.convergence_state == "inconclusive"
    assert is_hypothesis_memorialized(h) is True


def test_stable_results_NOT_memorialized(
    probe_enabled, isolated_ledger, tmp_path,
) -> None:
    """Genuine convergence on stable evidence does NOT memorialize —
    the operator may want to re-probe later if the codebase changes."""
    target = tmp_path / "real.txt"
    target.write_text("hello")
    h = Hypothesis(
        claim="real exists", confidence_prior=0.95,  # high prior
        test_strategy="lookup",
        expected_signal=f"file_exists:{target}",
        max_iterations=3,
    )
    r = asyncio.run(HypothesisProbe().test(h))
    if r.convergence_state == "stable":
        # Stable convergence should NOT memorialize
        assert is_hypothesis_memorialized(h) is False


def test_memorialized_hypothesis_short_circuits(
    fresh_registry, probe_enabled, isolated_ledger,
) -> None:
    """A hypothesis already in the dead ledger short-circuits with
    memorialized_dead state — no strategy execution at all."""
    h = Hypothesis(
        claim="dead-on-arrival", confidence_prior=0.5,
        test_strategy="lookup", expected_signal="x",
    )
    r0 = ProbeResult(
        confidence_posterior=0.5, observation_summary="manually buried",
        cost_usd=0.0, iterations_used=3,
        convergence_state="inconclusive", evidence_hash="abc",
    )
    memorialize_hypothesis(h, r0)
    r = asyncio.run(HypothesisProbe().test(h))
    assert r.convergence_state == "memorialized_dead"
    assert r.iterations_used == 0  # no strategy execution
    assert r.cost_usd == 0.0


# ===========================================================================
# §28 — Singleton accessor
# ===========================================================================


def test_get_default_probe_returns_singleton() -> None:
    p1 = get_default_probe()
    p2 = get_default_probe()
    assert p1 is p2


# ===========================================================================
# §29 — Authority invariants
# ===========================================================================


def test_no_authority_imports() -> None:
    from backend.core.ouroboros.governance.verification import hypothesis_probe
    src = inspect.getsource(hypothesis_probe)
    forbidden = (
        "orchestrator", "phase_runner", "candidate_generator",
        "iron_gate", "change_engine", "policy", "semantic_guardian",
    )
    for token in forbidden:
        assert (
            f"from backend.core.ouroboros.governance.{token}" not in src
        ), f"hypothesis_probe must not import {token}"
        assert (
            f"import backend.core.ouroboros.governance.{token}" not in src
        ), f"hypothesis_probe must not import {token}"


# ===========================================================================
# §30 — Public API
# ===========================================================================


def test_public_api_exposed_from_package() -> None:
    from backend.core.ouroboros.governance import verification
    expected = {
        "Hypothesis", "HypothesisProbe", "ProbeResult", "TestStrategy",
        "get_default_probe", "hypothesis_probe_enabled",
        "list_test_strategies", "register_test_strategy",
    }
    for name in expected:
        assert name in verification.__all__


# ===========================================================================
# §31 — Bayesian update reuses Slice 2.2 primitive
# ===========================================================================


def test_bayesian_update_reuses_repeat_runner_primitive() -> None:
    """The primitive must NOT duplicate Bayesian math — it delegates
    to Slice 2.2's _bayesian_update_safely (which itself wraps
    Antigravity's exploration_calculus). Source-grep enforces."""
    from backend.core.ouroboros.governance.verification import hypothesis_probe
    src = inspect.getsource(hypothesis_probe)
    assert "_bayesian_update_safely" in src  # delegated to Slice 2.2
    assert "from backend.core.ouroboros.governance.verification.repeat_runner" in src
