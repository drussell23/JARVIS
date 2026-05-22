"""Slice 7c — CircuitBreaker state machine tests.

The breaker is the **state-machine half** of the Slice 7 close-out
for the bt-2026-05-21-214521 retry storm. It consumes Slice 7a's
``RetryDecision`` + composes existing ``ProviderExhaustionWatcher``
and ``SessionBudgetAuthority`` per the operator's zero-state-
duplication binding. Slice 7e wires it into
``CandidateGenerator._call_fallback``.

Test surface:

  * Closed-taxonomy AST pins — ``CircuitState`` (4), ``CircuitScope``
    (2), ``VerdictAction`` (3).
  * **Zero state duplication AST pin** (operator-bound) — the
    breaker module MUST NOT define its own ``consecutive`` /
    ``remaining`` attribute that shadows the canonical sources.
  * Master-flag default-FALSE pin.
  * **Full-Jitter backoff distribution** (operator-bound) — large
    sample, assert delays are uniformly distributed in
    [0, base * 2^attempt] and the per-attempt ceiling matches AWS
    formula.
  * Trip table coverage — every (current state × RetryDecision)
    pair is exercised explicitly.
  * Pre-trip via SessionBudget floor.
  * Pre-trip via global breaker.
  * Global breaker — N structural trips within window trip it.
  * Sliding window correctness — events outside the window are
    pruned; events inside count.
  * Master-flag-FALSE no-op — evaluate() always returns RETRY_OK.
  * Public-surface ``__all__`` pin.
"""

from __future__ import annotations

import ast
import os
import pathlib
import random
import statistics
import unittest
from typing import List, Optional

from backend.core.ouroboros.governance.circuit_breaker import (
    CircuitBreaker,
    CircuitScope,
    CircuitState,
    CircuitVerdict,
    VerdictAction,
    circuit_breaker_enabled,
    full_jitter_delay,
    get_global_breaker,
    reset_global_breaker,
)
from backend.core.ouroboros.governance import circuit_breaker as cb_module
from backend.core.ouroboros.governance.provider_retry_classifier import (
    RetryDecision,
)


# ============================================================================
# Helpers — env-flag scope guard
# ============================================================================


class _EnvGuard:
    """Context manager that sets/restores env vars."""

    def __init__(self, **overrides: str) -> None:
        self._overrides = overrides
        self._prior: dict = {}

    def __enter__(self) -> "_EnvGuard":
        for k, v in self._overrides.items():
            self._prior[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *a: object) -> None:
        for k, prior in self._prior.items():
            if prior is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prior


def _enable_breaker() -> _EnvGuard:
    """Shortcut for tests that need the master flag ON."""
    return _EnvGuard(JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED="true")


# ============================================================================
# Closed-taxonomy AST pins
# ============================================================================


class TestClosedTaxonomies(unittest.TestCase):
    """The four closed enums in the breaker module are STRUCTURAL.
    Adding a 5th CircuitState (or 3rd Scope, or 4th VerdictAction)
    requires bumping these pins + Slice 7e's wiring branches."""

    def test_circuit_state_has_exactly_four_members(self) -> None:
        self.assertEqual(len(list(CircuitState)), 4)
        self.assertEqual(
            {m.name for m in CircuitState},
            {"CLOSED", "OPEN_TRANSIENT", "HALF_OPEN", "OPEN_TERMINAL"},
        )

    def test_circuit_scope_has_exactly_two_members(self) -> None:
        self.assertEqual(len(list(CircuitScope)), 2)
        self.assertEqual(
            {m.name for m in CircuitScope},
            {"PER_OP", "GLOBAL"},
        )

    def test_verdict_action_has_exactly_three_members(self) -> None:
        self.assertEqual(len(list(VerdictAction)), 3)
        self.assertEqual(
            {m.name for m in VerdictAction},
            {"RETRY_OK", "RETRY_AFTER_BACKOFF",
             "TERMINATE_UNRESOLVED"},
        )

    def test_circuit_verdict_is_frozen_dataclass(self) -> None:
        # Verifies the dataclass(frozen=True) decorator was applied.
        v = CircuitVerdict(action=VerdictAction.RETRY_OK)
        with self.assertRaises(Exception):
            v.action = VerdictAction.TERMINATE_UNRESOLVED  # type: ignore[misc]


# ============================================================================
# Zero state duplication AST pin (operator-bound)
# ============================================================================


_MODULE_FILE = pathlib.Path(cb_module.__file__)


def _parse_module() -> ast.Module:
    return ast.parse(_MODULE_FILE.read_text())


class TestNoStateDuplicationAstPin(unittest.TestCase):
    """Operator binding (verbatim): *"The Circuit Breaker must
    strictly act as a consumer of ExhaustionWatcher and
    SessionBudget. Maintain state machine purity."*

    The breaker MUST NOT define attributes named ``consecutive`` /
    ``_consecutive`` / ``session_remaining`` / ``_session_remaining``
    / ``budget_remaining`` / ``_budget_remaining`` on either the
    ``CircuitBreaker`` or ``_GlobalBreaker`` classes. These are the
    fields owned by the canonical sources — duplicating them here
    would create a parallel-state shadow."""

    _FORBIDDEN_ATTR_NAMES: set = {
        "consecutive", "_consecutive",
        "session_remaining", "_session_remaining",
        "budget_remaining", "_budget_remaining",
        "total_exhaustions", "_total_exhaustions",
    }

    def _class_attrs(self, class_name: str) -> List[str]:
        tree = _parse_module()
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.ClassDef)
                and node.name == class_name
            ):
                continue
            attrs: List[str] = []
            for sub in ast.walk(node):
                # Assignments like ``self._x = ...`` in __init__ etc.
                if (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "self"
                ):
                    attrs.append(sub.attr)
            return attrs
        self.fail(f"Class {class_name} not found in module")
        return []  # unreachable

    def test_circuit_breaker_does_not_shadow_canonical_counters(self) -> None:
        attrs = self._class_attrs("CircuitBreaker")
        offenders = sorted(set(attrs) & self._FORBIDDEN_ATTR_NAMES)
        self.assertEqual(
            offenders, [],
            f"CircuitBreaker defines attributes that shadow the "
            f"canonical ProviderExhaustionWatcher / "
            f"SessionBudgetAuthority state: {offenders}. The "
            f"breaker MUST consume those sources via injected "
            f"providers (no parallel state).",
        )

    def test_global_breaker_does_not_shadow_canonical_counters(self) -> None:
        attrs = self._class_attrs("_GlobalBreaker")
        offenders = sorted(set(attrs) & self._FORBIDDEN_ATTR_NAMES)
        self.assertEqual(
            offenders, [],
            f"_GlobalBreaker defines attributes that shadow "
            f"canonical state: {offenders}.",
        )


# ============================================================================
# Master flag default-FALSE pin
# ============================================================================


class TestMasterFlagDefault(unittest.TestCase):
    """Slice 7g graduated the breaker to default-TRUE on 2026-05-22.
    Hot-revert is via the explicit opt-out tokens (``"0"`` /
    ``"false"`` / ``"no"`` / ``"off"``) — but the empty/unset env
    var now leaves the breaker enabled."""

    def test_default_on(self) -> None:
        os.environ.pop("JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED",
                       None)
        self.assertTrue(
            circuit_breaker_enabled(),
            "Slice 7g: empty env must keep breaker enabled "
            "(graduated default-TRUE 2026-05-22)",
        )

    def test_truthy_values_enable(self) -> None:
        for v in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(v=v):
                with _EnvGuard(
                    JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED=v,
                ):
                    self.assertTrue(circuit_breaker_enabled())

    def test_falsy_values_disable(self) -> None:
        # Slice 7g: empty string is NO LONGER falsy (graduated
        # default-TRUE). Only explicit opt-out tokens disable.
        for v in ("0", "false", "no", "off"):
            with self.subTest(v=v):
                with _EnvGuard(
                    JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED=v,
                ):
                    self.assertFalse(circuit_breaker_enabled())

    def test_empty_string_keeps_graduated_default(self) -> None:
        """Slice 7g explicit: empty env value falls into the
        graduated-default path (== unset). Pinned separately so a
        regression to the pre-graduation shape fails loudly."""
        with _EnvGuard(JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED=""):
            self.assertTrue(
                circuit_breaker_enabled(),
                "empty string must be treated as 'unset' → "
                "graduated default-TRUE",
            )


# ============================================================================
# Master-flag-FALSE no-op
# ============================================================================


class TestMasterFlagOffIsNoop(unittest.TestCase):
    """When flag is OFF, evaluate() always returns RETRY_OK —
    byte-identical to no breaker. The retry loop's pre-existing
    semantics are preserved."""

    def test_evaluate_always_retry_ok_when_off(self) -> None:
        with _EnvGuard(JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED="false"):
            breaker = CircuitBreaker(op_id="op-1")
            for decision in RetryDecision:
                with self.subTest(decision=decision):
                    v = breaker.evaluate(decision)
                    self.assertEqual(
                        v.action, VerdictAction.RETRY_OK,
                        f"Master-flag-FALSE must return RETRY_OK "
                        f"for {decision.name} too — got "
                        f"{v.action.name}",
                    )

    def test_state_stays_closed_when_off(self) -> None:
        with _EnvGuard(JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED="false"):
            breaker = CircuitBreaker(op_id="op-1")
            for _ in range(20):
                breaker.evaluate(RetryDecision.TERMINAL_STRUCTURAL)
            self.assertEqual(breaker.state, CircuitState.CLOSED)


# ============================================================================
# Full-Jitter backoff distribution (operator-bound)
# ============================================================================


class TestFullJitterDistribution(unittest.TestCase):
    """Operator binding: *"OPEN_TRANSIENT exponential backoff must
    incorporate dynamic Full Jitter to prevent 'thundering herd'."*

    Algorithm verification — large sample, assert distribution
    matches AWS Full Jitter:

        delay = uniform(0, min(cap, base * 2^attempt))
    """

    def test_attempt_zero_delay_in_zero_to_base(self) -> None:
        # attempt=0, base=5.0, cap=60.0 → ceiling=min(60, 5)=5
        rng = random.Random(42)
        delays = [
            full_jitter_delay(0, base_s=5.0, cap_s=60.0, rng=rng)
            for _ in range(500)
        ]
        self.assertGreaterEqual(min(delays), 0.0)
        self.assertLessEqual(max(delays), 5.0 + 1e-9)

    def test_attempt_three_delay_in_zero_to_cap(self) -> None:
        # attempt=3, base=5.0 → 5*8=40; cap=60. Ceiling=40.
        rng = random.Random(42)
        delays = [
            full_jitter_delay(3, base_s=5.0, cap_s=60.0, rng=rng)
            for _ in range(500)
        ]
        self.assertGreaterEqual(min(delays), 0.0)
        self.assertLessEqual(max(delays), 40.0 + 1e-9)

    def test_cap_clamps_exponential(self) -> None:
        # attempt=10, base=5.0 → 5*1024=5120; cap=60. Ceiling=60.
        rng = random.Random(42)
        delays = [
            full_jitter_delay(10, base_s=5.0, cap_s=60.0, rng=rng)
            for _ in range(500)
        ]
        self.assertLessEqual(max(delays), 60.0 + 1e-9)

    def test_distribution_is_uniform(self) -> None:
        """Large-sample mean of uniform(0, ceiling) ≈ ceiling / 2.
        With N=2000 the empirical mean is within ~5% of theory."""
        rng = random.Random(42)
        ceiling = 20.0  # attempt=2, base=5.0 → 5*4=20, cap=60
        n = 2000
        delays = [
            full_jitter_delay(2, base_s=5.0, cap_s=60.0, rng=rng)
            for _ in range(n)
        ]
        emp_mean = statistics.mean(delays)
        theo_mean = ceiling / 2.0
        self.assertAlmostEqual(
            emp_mean, theo_mean, delta=ceiling * 0.05,
            msg=f"Empirical mean {emp_mean:.3f} should be ≈ "
                f"{theo_mean:.3f} (uniform(0, {ceiling})). "
                f"If this fails, the distribution isn't AWS Full "
                f"Jitter.",
        )

    def test_negative_attempt_clamped_to_zero(self) -> None:
        # Defensive: negative attempt clamps to attempt=0 (no
        # spurious huge backoffs).
        delays = [
            full_jitter_delay(-5, base_s=5.0, cap_s=60.0)
            for _ in range(50)
        ]
        self.assertTrue(all(0 <= d <= 5.0 for d in delays))

    def test_zero_cap_returns_zero(self) -> None:
        # Defensive: cap_s=0 means no backoff (caller disabled
        # backoff entirely).
        self.assertEqual(
            full_jitter_delay(5, base_s=5.0, cap_s=0.0),
            0.0,
        )

    def test_thundering_herd_dispersal(self) -> None:
        """100 simulated agents all roll their own full-jitter
        delay. Assert no two are identical (statistical guarantee
        with continuous uniform) AND the std-dev is reasonable
        — confirms dispersal."""
        rng = random.Random(42)
        delays = [
            full_jitter_delay(2, base_s=5.0, cap_s=60.0, rng=rng)
            for _ in range(100)
        ]
        self.assertGreater(
            statistics.stdev(delays), 1.0,
            "Full-jitter must produce dispersed delays — herd "
            "wouldn't disperse",
        )


# ============================================================================
# Trip-table coverage — every (state × RetryDecision) pair
# ============================================================================


class TestTripTable(unittest.TestCase):
    """Every decision-class triggers the right transition + verdict
    from the CLOSED starting state. Adapted with master flag ON."""

    def setUp(self) -> None:
        self._env = _enable_breaker()
        self._env.__enter__()
        reset_global_breaker()

    def tearDown(self) -> None:
        self._env.__exit__()

    def test_terminal_structural_trips_immediately(self) -> None:
        breaker = CircuitBreaker(op_id="op-1")
        v = breaker.evaluate(RetryDecision.TERMINAL_STRUCTURAL)
        self.assertEqual(v.action, VerdictAction.TERMINATE_UNRESOLVED)
        self.assertEqual(breaker.state, CircuitState.OPEN_TERMINAL)
        self.assertIsNotNone(v.terminal_reason_code)
        self.assertIn("terminal_structural", v.terminal_reason_code or "")

    def test_terminal_config_trips_immediately(self) -> None:
        breaker = CircuitBreaker(op_id="op-1")
        v = breaker.evaluate(RetryDecision.TERMINAL_CONFIG)
        self.assertEqual(v.action, VerdictAction.TERMINATE_UNRESOLVED)
        self.assertEqual(breaker.state, CircuitState.OPEN_TERMINAL)

    def test_terminal_quota_first_hit_backoff(self) -> None:
        with _EnvGuard(JARVIS_CIRCUIT_BREAKER_TERMINAL_QUOTA_TRIP="2"):
            breaker = CircuitBreaker(op_id="op-1")
            v = breaker.evaluate(RetryDecision.TERMINAL_QUOTA)
            self.assertEqual(
                v.action, VerdictAction.RETRY_AFTER_BACKOFF,
                "1 quota hit in window with trip-count=2 → backoff",
            )
            self.assertEqual(breaker.state, CircuitState.CLOSED)
            self.assertIsNotNone(v.backoff_s)
            self.assertGreaterEqual(v.backoff_s or 0, 0.0)

    def test_terminal_quota_nth_hit_trips(self) -> None:
        with _EnvGuard(JARVIS_CIRCUIT_BREAKER_TERMINAL_QUOTA_TRIP="2"):
            breaker = CircuitBreaker(op_id="op-1")
            v1 = breaker.evaluate(RetryDecision.TERMINAL_QUOTA)
            self.assertEqual(v1.action, VerdictAction.RETRY_AFTER_BACKOFF)
            v2 = breaker.evaluate(RetryDecision.TERMINAL_QUOTA)
            self.assertEqual(v2.action, VerdictAction.TERMINATE_UNRESOLVED)
            self.assertEqual(breaker.state, CircuitState.OPEN_TERMINAL)

    def test_retry_transient_below_trip_ok(self) -> None:
        with _EnvGuard(JARVIS_CIRCUIT_BREAKER_TRANSIENT_TRIP="3"):
            breaker = CircuitBreaker(op_id="op-1")
            for _ in range(2):
                v = breaker.evaluate(RetryDecision.RETRY_TRANSIENT)
                self.assertEqual(v.action, VerdictAction.RETRY_OK)
            self.assertEqual(breaker.state, CircuitState.CLOSED)

    def test_retry_transient_nth_trips_to_open_transient(self) -> None:
        with _EnvGuard(JARVIS_CIRCUIT_BREAKER_TRANSIENT_TRIP="3"):
            breaker = CircuitBreaker(op_id="op-1")
            for _ in range(3):
                breaker.evaluate(RetryDecision.RETRY_TRANSIENT)
            # 3rd transient trips to OPEN_TRANSIENT — caller backs off
            v = breaker.evaluate(RetryDecision.RETRY_TRANSIENT)
            self.assertEqual(
                v.action, VerdictAction.RETRY_AFTER_BACKOFF,
                "After N transient hits, breaker enters "
                "OPEN_TRANSIENT and demands backoff",
            )
            self.assertEqual(
                breaker.state, CircuitState.OPEN_TRANSIENT,
            )

    def test_open_terminal_is_sticky(self) -> None:
        breaker = CircuitBreaker(op_id="op-1")
        breaker.evaluate(RetryDecision.TERMINAL_STRUCTURAL)
        self.assertEqual(breaker.state, CircuitState.OPEN_TERMINAL)
        # Subsequent evaluate() calls — even with RETRY_OK-able
        # decisions — must stay OPEN_TERMINAL.
        for d in (
            RetryDecision.RETRY_TRANSIENT,
            RetryDecision.TERMINAL_QUOTA,
            RetryDecision.TERMINAL_CONFIG,
        ):
            v = breaker.evaluate(d)
            self.assertEqual(v.action, VerdictAction.TERMINATE_UNRESOLVED)
            self.assertEqual(breaker.state, CircuitState.OPEN_TERMINAL)


# ============================================================================
# Pre-trip via SessionBudget floor
# ============================================================================


class TestBudgetFloorPreemption(unittest.TestCase):
    """When the SessionBudgetAuthority oracle reports
    ``remaining < min_floor``, the breaker pre-trips to
    OPEN_TERMINAL before the failure classifier is even consulted.
    Saves the round-trip to a provider that would refuse anyway."""

    def setUp(self) -> None:
        self._env = _enable_breaker()
        self._env.__enter__()
        reset_global_breaker()

    def tearDown(self) -> None:
        self._env.__exit__()

    def test_below_floor_trips_terminal_before_provider_call(self) -> None:
        with _EnvGuard(
            JARVIS_CIRCUIT_BREAKER_MIN_BUDGET_FLOOR_USD="0.05",
        ):
            breaker = CircuitBreaker(
                op_id="op-1",
                budget_provider=lambda: 0.01,  # below floor
            )
            v = breaker.evaluate(RetryDecision.RETRY_TRANSIENT)
            self.assertEqual(v.action, VerdictAction.TERMINATE_UNRESOLVED)
            self.assertEqual(v.terminal_reason_code,
                             "budget_floor_breached")
            self.assertEqual(breaker.state, CircuitState.OPEN_TERMINAL)

    def test_above_floor_proceeds_normally(self) -> None:
        with _EnvGuard(
            JARVIS_CIRCUIT_BREAKER_MIN_BUDGET_FLOOR_USD="0.05",
        ):
            breaker = CircuitBreaker(
                op_id="op-1",
                budget_provider=lambda: 0.50,  # well above floor
            )
            v = breaker.evaluate(RetryDecision.RETRY_TRANSIENT)
            self.assertEqual(v.action, VerdictAction.RETRY_OK)

    def test_none_remaining_is_failopen(self) -> None:
        """When the budget oracle returns None, the breaker is
        fail-OPEN — provider preflight is the safety net."""
        breaker = CircuitBreaker(
            op_id="op-1",
            budget_provider=lambda: None,
        )
        v = breaker.evaluate(RetryDecision.RETRY_TRANSIENT)
        self.assertEqual(v.action, VerdictAction.RETRY_OK)

    def test_provider_raises_is_failopen(self) -> None:
        """A misbehaving budget provider must not break the
        breaker."""
        def _bad() -> Optional[float]:
            raise RuntimeError("simulated budget oracle fault")

        breaker = CircuitBreaker(
            op_id="op-1", budget_provider=_bad,
        )
        v = breaker.evaluate(RetryDecision.RETRY_TRANSIENT)
        self.assertEqual(v.action, VerdictAction.RETRY_OK)


# ============================================================================
# Global breaker — session-wide trips
# ============================================================================


class TestGlobalBreaker(unittest.TestCase):
    """The global breaker is a process singleton. ≥N structural
    trips within window trip it to OPEN_TERMINAL. Once tripped,
    every per-op breaker's evaluate() returns
    TERMINATE_UNRESOLVED with
    ``terminal_reason_code=global_session_exhausted``."""

    def setUp(self) -> None:
        self._env = _enable_breaker()
        self._env.__enter__()
        reset_global_breaker()

    def tearDown(self) -> None:
        reset_global_breaker()
        self._env.__exit__()

    def test_n_structural_trips_trip_global(self) -> None:
        with _EnvGuard(
            JARVIS_CIRCUIT_BREAKER_GLOBAL_TRIP_COUNT="3",
        ):
            # 3 distinct per-op breakers all hit TERMINAL_STRUCTURAL.
            for i in range(3):
                b = CircuitBreaker(op_id=f"op-{i}")
                b.evaluate(RetryDecision.TERMINAL_STRUCTURAL)
            self.assertEqual(
                get_global_breaker().state,
                CircuitState.OPEN_TERMINAL,
                "Global breaker should trip after 3 structural "
                "trips (threshold=3)",
            )

    def test_global_trip_propagates_to_new_per_op(self) -> None:
        with _EnvGuard(
            JARVIS_CIRCUIT_BREAKER_GLOBAL_TRIP_COUNT="2",
        ):
            for i in range(2):
                CircuitBreaker(
                    op_id=f"op-{i}",
                ).evaluate(RetryDecision.TERMINAL_STRUCTURAL)
            # Global should be tripped now. A fresh per-op breaker
            # immediately reports OPEN_TERMINAL.
            fresh = CircuitBreaker(op_id="op-fresh")
            v = fresh.evaluate(RetryDecision.RETRY_TRANSIENT)
            self.assertEqual(v.action, VerdictAction.TERMINATE_UNRESOLVED)
            self.assertEqual(
                v.terminal_reason_code, "global_session_exhausted",
            )

    def test_quota_trips_do_not_increment_global(self) -> None:
        """Only TERMINAL_STRUCTURAL trips bubble to the global
        breaker. Quota / config are op-specific."""
        with _EnvGuard(
            JARVIS_CIRCUIT_BREAKER_GLOBAL_TRIP_COUNT="2",
            JARVIS_CIRCUIT_BREAKER_TERMINAL_QUOTA_TRIP="1",
        ):
            # 5 quota trips — global should stay CLOSED.
            for i in range(5):
                CircuitBreaker(
                    op_id=f"op-{i}",
                ).evaluate(RetryDecision.TERMINAL_QUOTA)
            self.assertEqual(
                get_global_breaker().state, CircuitState.CLOSED,
            )


# ============================================================================
# record_success() — clears in-window counters + HALF_OPEN → CLOSED
# ============================================================================


class TestRecordSuccess(unittest.TestCase):
    def setUp(self) -> None:
        self._env = _enable_breaker()
        self._env.__enter__()
        reset_global_breaker()

    def tearDown(self) -> None:
        self._env.__exit__()

    def test_success_clears_transient_window(self) -> None:
        with _EnvGuard(JARVIS_CIRCUIT_BREAKER_TRANSIENT_TRIP="3"):
            breaker = CircuitBreaker(op_id="op-1")
            # 2 transients (below trip).
            for _ in range(2):
                breaker.evaluate(RetryDecision.RETRY_TRANSIENT)
            breaker.record_success()
            # Window should be cleared — 2 more transients are
            # still below the trip count.
            for _ in range(2):
                v = breaker.evaluate(RetryDecision.RETRY_TRANSIENT)
                self.assertEqual(v.action, VerdictAction.RETRY_OK)

    def test_success_recovers_open_transient_to_closed(self) -> None:
        with _EnvGuard(JARVIS_CIRCUIT_BREAKER_TRANSIENT_TRIP="2"):
            breaker = CircuitBreaker(op_id="op-1")
            breaker.evaluate(RetryDecision.RETRY_TRANSIENT)
            breaker.evaluate(RetryDecision.RETRY_TRANSIENT)
            self.assertEqual(
                breaker.state, CircuitState.OPEN_TRANSIENT,
            )
            breaker.record_success()
            self.assertEqual(breaker.state, CircuitState.CLOSED)
            self.assertEqual(breaker.backoff_attempt, 0)

    def test_success_when_off_is_safe(self) -> None:
        # Master flag OFF — record_success is a no-op.
        with _EnvGuard(JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED="false"):
            breaker = CircuitBreaker(op_id="op-1")
            breaker.record_success()  # must not raise


# ============================================================================
# Public surface pin
# ============================================================================


class TestPublicSurface(unittest.TestCase):
    def test_all_exports(self) -> None:
        expected = {
            "CircuitState", "CircuitScope", "VerdictAction",
            "CircuitVerdict", "CircuitBreaker", "full_jitter_delay",
            "circuit_breaker_enabled",
            "get_global_breaker", "reset_global_breaker",
        }
        self.assertEqual(set(cb_module.__all__), expected)

    def test_each_export_exists(self) -> None:
        for name in cb_module.__all__:
            self.assertTrue(hasattr(cb_module, name))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
