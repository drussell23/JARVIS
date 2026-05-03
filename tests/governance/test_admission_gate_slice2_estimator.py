"""AdmissionGate Slice 2 — WaitTimeEstimator regression suite.

Pins the rolling EWMA estimator that feeds the admission gate's
``projected_wait_s`` input. Slice 2's CandidateGenerator
integration is tested separately in
``test_admission_gate_slice2_integration.py`` because it requires
an aiosrc CandidateGenerator instance with full provider
plumbing.

Strict directives validated:

  * NEVER raises into callers — adversarial-input matrix verified
  * Thread-safe — concurrent stress test verifies no torn state
  * Memory-bounded — per-route EWMA dict; routes are an enum so
    at most 5-6 entries ever
  * Pure-stdlib — AST pin asserts no backend.* / asyncio imports
  * Cold-start sane — first observation per route initializes at
    the observed value (no decay from fictitious zero-prior)

Covers:

  §A   Schema version + env knob defaults / clamps
  §B   Cold-start: project_wait returns 0.0 with no observations
  §C   First observation initializes EWMA at observed value
  §D   Subsequent observations apply EWMA formula correctly
  §E   Per-route isolation (different routes don't bleed)
  §F   Garbage input handling (NaN, negative, None, non-numeric)
  §G   Sample count tracking
  §H   Reset clears all state
  §I   stats() shape includes alpha + per-route + sample counts
  §J   Concurrent update + read stress test
  §K   AST authority pins (no asyncio, no backend.*, no caller modules)
"""
from __future__ import annotations

import ast
import inspect
import math
import threading
from typing import List

import pytest

from backend.core.ouroboros.governance.admission_estimator import (
    ADMISSION_ESTIMATOR_SCHEMA_VERSION,
    WaitTimeEstimator,
    estimator_alpha,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_ADMISSION_ESTIMATOR_ALPHA", raising=False,
    )
    yield


# ---------------------------------------------------------------------------
# §A — Schema + env knob
# ---------------------------------------------------------------------------


class TestSchemaAndEnvKnob:
    def test_schema_version_pin(self):
        assert (
            ADMISSION_ESTIMATOR_SCHEMA_VERSION
            == "admission_estimator.v1"
        )

    def test_alpha_default(self):
        assert estimator_alpha() == 0.3

    def test_alpha_floor_clamp(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_ESTIMATOR_ALPHA", "0.001",
        )
        assert estimator_alpha() == 0.05

    def test_alpha_ceiling_clamp(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_ESTIMATOR_ALPHA", "1.5",
        )
        assert estimator_alpha() == 0.95

    def test_alpha_garbage_falls_to_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_ESTIMATOR_ALPHA", "abc",
        )
        assert estimator_alpha() == 0.3

    def test_constructor_accepts_alpha_override(self):
        est = WaitTimeEstimator(alpha=0.7)
        assert est.alpha == 0.7

    def test_constructor_clamps_alpha_override(self):
        est_low = WaitTimeEstimator(alpha=0.001)
        assert est_low.alpha == 0.05
        est_high = WaitTimeEstimator(alpha=99)
        assert est_high.alpha == 0.95

    def test_constructor_defaults_to_env(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_ESTIMATOR_ALPHA", "0.5",
        )
        est = WaitTimeEstimator()
        assert est.alpha == 0.5


# ---------------------------------------------------------------------------
# §B — Cold start
# ---------------------------------------------------------------------------


class TestColdStart:
    def test_project_wait_zero_for_unknown_route(self):
        est = WaitTimeEstimator()
        assert est.project_wait("immediate") == 0.0
        assert est.project_wait("standard") == 0.0
        assert est.project_wait("anything") == 0.0

    def test_project_wait_zero_for_empty_route(self):
        est = WaitTimeEstimator()
        assert est.project_wait("") == 0.0
        assert est.project_wait(None) == 0.0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# §C – §D — EWMA math
# ---------------------------------------------------------------------------


class TestEWMAMath:
    def test_first_observation_initializes_at_observed_value(self):
        est = WaitTimeEstimator(alpha=0.3)
        est.update_observed("immediate", 50.0)
        assert est.project_wait("immediate") == 50.0

    def test_second_observation_applies_ewma_formula(self):
        # alpha=0.3, prev=50, obs=100
        # new = 0.3 * 100 + 0.7 * 50 = 30 + 35 = 65
        est = WaitTimeEstimator(alpha=0.3)
        est.update_observed("immediate", 50.0)
        est.update_observed("immediate", 100.0)
        assert est.project_wait("immediate") == pytest.approx(65.0)

    def test_third_observation_applies_ewma_formula(self):
        # alpha=0.3
        # obs1=50 → ewma=50
        # obs2=100 → ewma=0.3*100+0.7*50=65
        # obs3=20 → ewma=0.3*20+0.7*65=6+45.5=51.5
        est = WaitTimeEstimator(alpha=0.3)
        est.update_observed("immediate", 50.0)
        est.update_observed("immediate", 100.0)
        est.update_observed("immediate", 20.0)
        assert est.project_wait("immediate") == pytest.approx(51.5)

    def test_high_alpha_responds_quickly(self):
        # alpha=0.9 → almost-fully tracks the latest observation
        est = WaitTimeEstimator(alpha=0.9)
        est.update_observed("immediate", 50.0)
        est.update_observed("immediate", 200.0)
        # 0.9*200 + 0.1*50 = 180 + 5 = 185
        assert est.project_wait("immediate") == pytest.approx(185.0)

    def test_low_alpha_responds_slowly(self):
        # alpha=0.05 → barely shifts on new obs
        est = WaitTimeEstimator(alpha=0.05)
        est.update_observed("immediate", 50.0)
        est.update_observed("immediate", 200.0)
        # 0.05*200 + 0.95*50 = 10 + 47.5 = 57.5
        assert est.project_wait("immediate") == pytest.approx(57.5)

    def test_zero_observation_pulls_ewma_down(self):
        est = WaitTimeEstimator(alpha=0.3)
        est.update_observed("immediate", 100.0)
        est.update_observed("immediate", 0.0)
        # 0.3*0 + 0.7*100 = 70
        assert est.project_wait("immediate") == pytest.approx(70.0)


# ---------------------------------------------------------------------------
# §E — Per-route isolation
# ---------------------------------------------------------------------------


class TestPerRouteIsolation:
    def test_routes_are_independent(self):
        est = WaitTimeEstimator(alpha=0.3)
        est.update_observed("immediate", 100.0)
        est.update_observed("standard", 50.0)
        est.update_observed("background", 10.0)
        assert est.project_wait("immediate") == 100.0
        assert est.project_wait("standard") == 50.0
        assert est.project_wait("background") == 10.0
        # Unknown route still returns 0.0
        assert est.project_wait("complex") == 0.0

    def test_route_normalization_case_insensitive(self):
        # Routes normalized to lower-case + stripped, so
        # "IMMEDIATE" and "immediate" map to the same bucket.
        est = WaitTimeEstimator()
        est.update_observed("IMMEDIATE", 100.0)
        assert est.project_wait("immediate") == 100.0
        assert est.project_wait("  Immediate  ") == 100.0


# ---------------------------------------------------------------------------
# §F — Garbage input
# ---------------------------------------------------------------------------


class TestGarbageInput:
    def test_nan_observation_silently_dropped(self):
        est = WaitTimeEstimator()
        est.update_observed("immediate", 50.0)
        est.update_observed("immediate", float("nan"))
        # Still 50 — NaN dropped without affecting state.
        assert est.project_wait("immediate") == 50.0

    def test_negative_observation_silently_dropped(self):
        est = WaitTimeEstimator()
        est.update_observed("immediate", 50.0)
        est.update_observed("immediate", -10.0)
        assert est.project_wait("immediate") == 50.0

    def test_non_numeric_observation_silently_dropped(self):
        est = WaitTimeEstimator()
        est.update_observed("immediate", 50.0)
        est.update_observed("immediate", "not a number")  # type: ignore[arg-type]
        assert est.project_wait("immediate") == 50.0

    def test_none_route_silently_dropped(self):
        est = WaitTimeEstimator()
        est.update_observed(None, 50.0)  # type: ignore[arg-type]
        # No state — projecting returns 0.
        assert est.project_wait(None) == 0.0  # type: ignore[arg-type]

    def test_empty_route_silently_dropped(self):
        est = WaitTimeEstimator()
        est.update_observed("", 50.0)
        est.update_observed("   ", 50.0)
        assert est.project_wait("") == 0.0


# ---------------------------------------------------------------------------
# §G — Sample count tracking
# ---------------------------------------------------------------------------


class TestSampleCounts:
    def test_sample_count_increments_on_valid_obs(self):
        est = WaitTimeEstimator()
        for _ in range(7):
            est.update_observed("immediate", 50.0)
        assert (
            est.stats()["sample_counts"]["immediate"] == 7
        )

    def test_sample_count_does_not_increment_on_garbage(self):
        est = WaitTimeEstimator()
        est.update_observed("immediate", 50.0)
        est.update_observed("immediate", float("nan"))
        est.update_observed("immediate", -1.0)
        est.update_observed("immediate", "x")  # type: ignore[arg-type]
        # Only the one valid obs counts
        assert (
            est.stats()["sample_counts"]["immediate"] == 1
        )

    def test_sample_counts_per_route(self):
        est = WaitTimeEstimator()
        for _ in range(3):
            est.update_observed("immediate", 10.0)
        for _ in range(5):
            est.update_observed("standard", 20.0)
        s = est.stats()
        assert s["sample_counts"]["immediate"] == 3
        assert s["sample_counts"]["standard"] == 5


# ---------------------------------------------------------------------------
# §H — Reset
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_state(self):
        est = WaitTimeEstimator()
        est.update_observed("immediate", 50.0)
        est.update_observed("standard", 100.0)
        est.reset()
        assert est.project_wait("immediate") == 0.0
        assert est.project_wait("standard") == 0.0
        assert est.stats()["sample_counts"] == {}


# ---------------------------------------------------------------------------
# §I — stats() shape
# ---------------------------------------------------------------------------


class TestStatsShape:
    def test_stats_has_required_keys(self):
        est = WaitTimeEstimator()
        s = est.stats()
        assert "alpha" in s
        assert "ewma_per_route_s" in s
        assert "sample_counts" in s
        assert "schema_version" in s
        assert (
            s["schema_version"]
            == ADMISSION_ESTIMATOR_SCHEMA_VERSION
        )

    def test_stats_reflects_observations(self):
        est = WaitTimeEstimator(alpha=0.3)
        est.update_observed("immediate", 50.0)
        est.update_observed("standard", 25.0)
        s = est.stats()
        assert s["alpha"] == 0.3
        assert s["ewma_per_route_s"]["immediate"] == 50.0
        assert s["ewma_per_route_s"]["standard"] == 25.0


# ---------------------------------------------------------------------------
# §J — Concurrent update + read stress test
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_updates_no_torn_state(self):
        # 20 threads × 100 iterations × 5 routes — verify no
        # exceptions, sample counts add up correctly, EWMA
        # values are within [0, max_obs] per route.
        est = WaitTimeEstimator(alpha=0.3)
        errors: List[Exception] = []
        N_THREADS = 20
        N_ITERS = 100
        ROUTES = ["immediate", "standard", "complex",
                  "background", "speculative"]

        def worker(tid: int):
            try:
                for i in range(N_ITERS):
                    route = ROUTES[i % len(ROUTES)]
                    obs = float((tid * N_ITERS + i) % 200)
                    est.update_observed(route, obs)
                    # Occasionally project to exercise read+write
                    # concurrency.
                    if i % 10 == 0:
                        est.project_wait(route)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,))
            for i in range(N_THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        s = est.stats()
        # Each route received N_THREADS × N_ITERS / 5 = 400 obs.
        for route in ROUTES:
            assert s["sample_counts"][route] == (
                N_THREADS * N_ITERS // len(ROUTES)
            )
            # EWMA bounded by [0, 200) — observations were 0..199
            ewma = s["ewma_per_route_s"][route]
            assert 0 <= ewma <= 199, (
                f"{route} EWMA {ewma} out of expected range"
            )

    def test_concurrent_reads_during_writes_safe(self):
        # Exercises the lock contract — readers don't see torn
        # state mid-write. Smoke test (real torn-state would
        # surface as KeyError or non-finite value).
        est = WaitTimeEstimator()

        def writer():
            for i in range(500):
                est.update_observed("immediate", float(i))

        def reader(out: List[float]):
            for _ in range(500):
                v = est.project_wait("immediate")
                if math.isnan(v) or v < 0:
                    out.append(v)

        bad: List[float] = []
        w = threading.Thread(target=writer)
        r1 = threading.Thread(target=reader, args=(bad,))
        r2 = threading.Thread(target=reader, args=(bad,))
        w.start(); r1.start(); r2.start()
        w.join(); r1.join(); r2.join()
        assert bad == []  # no torn / NaN reads


# ---------------------------------------------------------------------------
# §K — AST authority pins
# ---------------------------------------------------------------------------


class TestAuthorityPins:
    @staticmethod
    def _module_imports():
        from backend.core.ouroboros.governance import (
            admission_estimator,
        )
        src = inspect.getsource(admission_estimator)
        tree = ast.parse(src)
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                out.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    out.append(alias.name)
        return out

    def test_no_asyncio_import(self):
        for imp in self._module_imports():
            assert "asyncio" not in imp.split("."), (
                f"forbidden asyncio: {imp}"
            )

    def test_no_caller_imports(self):
        forbidden = {
            "candidate_generator", "providers",
            "orchestrator", "urgency_router",
            "iron_gate", "risk_tier", "change_engine",
            "gate", "yaml_writer", "policy",
        }
        for imp in self._module_imports():
            for f in forbidden:
                assert f not in imp.split("."), (
                    f"forbidden caller import: {imp}"
                )

    def test_no_backend_imports(self):
        for imp in self._module_imports():
            assert not imp.startswith("backend."), (
                f"non-stdlib import: {imp}"
            )
