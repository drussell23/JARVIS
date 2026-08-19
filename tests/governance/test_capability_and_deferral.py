"""A badge that cannot lie, and a blind spot that has a name.

Two defects, both observed live on 2026-08-18:

  1. The cockpit printed `● healthy` while both provider lanes were at zero
     credit and every op was failing. The header read
     `get_reactive_theme().state` — a PRESENTATION state answering "what
     colour should the dot be" — and rendered it as a CAPABILITY claim, on
     top of two optimistic defaults.
  2. A 6.7 KB file was logged as a 10 s AST "timeout" after 14.1 s of
     `parent_await_ms`. A 6.7 KB parse does not take 14 s; the file spent
     that time queued behind a saturated pool and was blamed for it. It was
     then dropped with no record of which files went unanalysed.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import ast_compile_helper as ah
from backend.core.ouroboros.governance import capability_state as cs
from backend.core.ouroboros.governance import deferred_task_ledger as dl


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DEFERRED_LEDGER_PATH", str(tmp_path / "d.json"))
    for k in ("JARVIS_CAPABILITY_STATE_ENABLED", "JARVIS_CAPABILITY_TTL_S",
              "JARVIS_CAPABILITY_FAIL_STREAK", "JARVIS_AST_MAX_BYTES_PER_LINE",
              "JARVIS_AST_DENSITY_FLOOR_BYTES", "JARVIS_AST_SHED_QUEUE_DEPTH"):
        monkeypatch.delenv(k, raising=False)
    cs.reset_for_tests()
    dl.reset_for_tests()
    yield
    cs.reset_for_tests()
    dl.reset_for_tests()


def _ev(monkeypatch, *, dry=False, provider="", lanes_ok=True,
        attempted=0, completed=0, failed=0, ops_ok=True):
    e = cs.CapabilityEvaluator()
    monkeypatch.setattr(cs.CapabilityEvaluator, "_read_lanes",
                        staticmethod(lambda: (dry, provider, lanes_ok)))
    monkeypatch.setattr(cs.CapabilityEvaluator, "_read_ops",
                        staticmethod(lambda: (attempted, completed, failed, ops_ok)))
    return e


class TestTheBadgeCannotClaimHealthItDoesNotHave:
    def test_a_dry_lane_blocks_immediately(self, monkeypatch):
        """A dry runway is a BILLING state, not jitter. Waiting for a streak
        would reproduce the original defect in slower motion."""
        r = _ev(monkeypatch, dry=True, provider="doubleword").evaluate()
        assert r.state is cs.Capability.BLOCKED
        assert "doubleword" in r.reason

    def test_unknown_renders_as_blocked_never_healthy(self, monkeypatch):
        """The inverted default. If nothing is readable we do not know whether
        the organism can work, and the honest rendering of 'I don't know' is
        not a green dot."""
        r = _ev(monkeypatch, lanes_ok=False, ops_ok=False).evaluate()
        assert r.state is cs.Capability.UNKNOWN
        assert r.badge == "blocked"

    def test_a_broken_fusion_still_refuses_to_claim_health(self, monkeypatch):
        monkeypatch.setattr(
            cs.CapabilityEvaluator, "_read_lanes",
            staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
        r = cs.CapabilityEvaluator().evaluate()
        assert r.badge == "blocked"

    def test_all_ops_failing_blocks(self, monkeypatch):
        r = _ev(monkeypatch, attempted=9, completed=0, failed=9).evaluate()
        assert r.state is cs.Capability.BLOCKED
        assert "9" in r.reason

    def test_some_failures_is_degraded_not_blocked(self, monkeypatch):
        r = _ev(monkeypatch, attempted=10, completed=7, failed=3).evaluate()
        assert r.state is cs.Capability.DEGRADED

    def test_clean_history_is_healthy(self, monkeypatch):
        r = _ev(monkeypatch, attempted=5, completed=5, failed=0).evaluate()
        assert r.state is cs.Capability.HEALTHY

    def test_it_no_longer_reads_the_ui_theme(self):
        """The category error. `UIState` answers 'what colour am I', and the
        header rendered that as 'can I work'.

        AST, not substring: this module's docstring NAMES `get_reactive_theme`
        in order to explain the defect it replaces, so a text search finds the
        prose and fails a module that is in fact clean. A test must be able to
        tell an explanation from a use.
        """
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(cs))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for n in ast.walk(tree):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                names |= {a.name.split(".")[-1] for a in n.names}
                names |= set((getattr(n, "module", "") or "").split("."))
        assert "get_reactive_theme" not in names
        assert "UIState" not in names
        assert "theme" not in names


class TestAsymmetricHysteresis:
    def test_recovery_requires_a_verified_success_not_a_timer(self, monkeypatch):
        """A lane flickering back to 'not exhausted' has proved nothing. An op
        that COMPLETED has."""
        e = _ev(monkeypatch, dry=True, provider="dw", attempted=4,
                completed=0, failed=4)
        assert e.evaluate().state is cs.Capability.BLOCKED

        # lane returns, but nothing has completed yet -> still blocked
        monkeypatch.setattr(cs.CapabilityEvaluator, "_read_lanes",
                            staticmethod(lambda: (False, "", True)))
        e.invalidate()
        held = e.evaluate()
        assert held.state is cs.Capability.BLOCKED
        assert held.held_by_hysteresis is True

        # an op completes -> recovery
        monkeypatch.setattr(cs.CapabilityEvaluator, "_read_ops",
                            staticmethod(lambda: (5, 1, 4, True)))
        e.invalidate()
        assert e.evaluate().state is cs.Capability.HEALTHY

    def test_degradation_does_not_wait_for_hysteresis(self, monkeypatch):
        """Asymmetric on purpose: cheap to admit trouble, expensive to claim
        health. Mirrors the profiler's asymmetric EWMA."""
        e = _ev(monkeypatch, attempted=3, completed=3, failed=0)
        assert e.evaluate().state is cs.Capability.HEALTHY
        monkeypatch.setattr(cs.CapabilityEvaluator, "_read_lanes",
                            staticmethod(lambda: (True, "dw", True)))
        e.invalidate()
        assert e.evaluate().state is cs.Capability.BLOCKED

    def test_the_master_flag_off_restores_legacy(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CAPABILITY_STATE_ENABLED", "0")
        assert cs.CapabilityEvaluator().evaluate().state is cs.Capability.HEALTHY


class TestTheHighEntropyGuard:
    def _run(self, source, name="f.py"):
        return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            ah.analyze_python_source_for_opportunity_miner(
                "test", source, filename=name))

    def test_a_minified_bundle_is_refused_by_shape_not_size(self):
        """60 KB on one line is under any byte cap and still pathological for
        an AST walk. Size cannot express this; density can."""
        r = self._run("x=1;" * 15000, "minified.py")
        assert r.outcome is ah.AnalyzeOutcome.PATHOLOGICAL
        assert "density" in (r.error_detail or "")

    def test_binary_content_is_refused(self):
        r = self._run("abc\x00def" * 2000, "blob.bin")
        assert r.outcome is ah.AnalyzeOutcome.PATHOLOGICAL

    def test_a_small_one_liner_is_not_punished(self):
        """A legitimate 11-byte one-liner has a terrible bytes-per-line ratio
        and costs nothing to parse. The density floor exists so the guard does
        not reject healthy files to prevent a cost that does not exist."""
        assert self._run("x=1;y=2;z=3", "tiny.py").outcome is ah.AnalyzeOutcome.OK

    def test_the_guard_runs_before_the_execution_branch(self):
        """Load-bearing placement: inline-tiny runs on the caller's thread and
        the Oracle runs in-process with no pool, so NEITHER can be cancelled
        by a timeout. Guarding after the branch would have protected only the
        path that was already protected."""
        import inspect
        src = inspect.getsource(ah.analyze_python_source_for_opportunity_miner)
        assert src.index("_pathological_shape") < src.index("source_bytes <= tiny_threshold")

    def test_thresholds_are_tunable_not_literal(self, monkeypatch):
        monkeypatch.setenv("JARVIS_AST_MAX_BYTES_PER_LINE", "5000")
        monkeypatch.setenv("JARVIS_AST_DENSITY_FLOOR_BYTES", "1000")
        assert ah._resolve_max_bytes_per_line() == 5000.0
        assert ah._resolve_density_floor_bytes() == 1000
        assert ah._pathological_shape("x=1;" * 200, 800) is None

    def test_the_guard_never_raises(self):
        for bad in (None, 12345, object()):
            try:
                ah._pathological_shape(bad, 9999)   # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"guard raised {exc!r}")


class TestTheDeferralLedger:
    def test_a_shape_rejection_is_recorded_and_not_retried(self):
        """Remembered, but re-running the same bytes through the same guard
        yields the same refusal — so it must not be re-queued unchanged."""
        led = dl.get_default_ledger()
        led.defer(path="a.py", reason=dl.DeferReason.PATHOLOGICAL)
        assert led.snapshot()["total"] == 1
        assert led.retryable() == []

    def test_pressure_and_timeout_are_retryable(self):
        led = dl.get_default_ledger()
        led.defer(path="b.py", reason=dl.DeferReason.SHED_PRESSURE)
        led.defer(path="c.py", reason=dl.DeferReason.TIMEOUT)
        assert {t.path for t in led.retryable()} == {"b.py", "c.py"}

    def test_re_shedding_updates_rather_than_appends(self):
        """A file shed on every scan is ONE persistent blind spot, not fifty
        events. Counting it fifty times would drown the ledger in its own
        worst case."""
        led = dl.get_default_ledger()
        for _ in range(50):
            led.defer(path="hot.py", reason=dl.DeferReason.SHED_PRESSURE)
        assert led.snapshot()["total"] == 1
        assert led.retryable()[0].occurrences == 50

    def test_success_resolves_the_blind_spot(self):
        led = dl.get_default_ledger()
        led.defer(path="d.py", reason=dl.DeferReason.SHED_PRESSURE)
        led.resolve("d.py")
        assert led.snapshot()["total"] == 0

    def test_eviction_is_counted_not_silent(self, monkeypatch):
        """A ledger that silently forgot entries would be a blind spot about
        blind spots."""
        monkeypatch.setenv("JARVIS_DEFERRED_LEDGER_MAX", "16")
        led = dl.DeferredTaskLedger()
        for i in range(40):
            led.defer(path=f"f{i}.py", reason=dl.DeferReason.SHED_PRESSURE,
                      now=float(i))
        snap = led.snapshot()
        assert snap["total"] == 16
        assert snap["evicted"] == 24

    def test_the_ledger_never_raises(self):
        led = dl.get_default_ledger()
        led.defer(path="", reason=dl.DeferReason.TIMEOUT)     # empty path
        led.resolve("nonexistent.py")
        assert isinstance(led.snapshot(), dict)


class TestInflightAccounting:
    def test_the_release_lives_in_the_analyze_function(self):
        """Regression pin. This file mirrors its parse and analyze
        implementations almost line-for-line; an anchored edit first landed
        the release in the PARSE twin, so analyze incremented and never
        released — the counter would climb until `_pool_saturated()` returned
        True forever, converting a backpressure valve into a permanent
        outage."""
        import inspect
        src = inspect.getsource(ah._process_pool_analyze)
        assert "_inflight += 1" in src
        assert "_inflight = max(0, _inflight - 1)" in src
        assert "finally:" in src

    def test_saturation_is_computed_not_read_from_a_private_field(self):
        import inspect
        src = inspect.getsource(ah._pool_saturated)
        assert "_pending_work_items" not in src
