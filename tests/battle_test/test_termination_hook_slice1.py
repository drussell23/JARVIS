"""TerminationHookRegistry Slice 1 — primitive regression suite.

Pins the pure-stdlib substrate that Slice 2 wraps with the
registry + auto-discovery, and that Slice 3 wires into the
harness's wall-clock + signal paths uniformly.

Strict directives validated:

  * Sync-first discipline: AST pin asserts the module imports
    NOTHING from asyncio.
  * Deterministic budgets: phase budget enforcement test +
    per-hook timeout enforcement test + the
    ``min(per_hook, remaining_budget)`` clamp pin.
  * NEVER raises: adversarial-input matrix collapses every
    failure mode to a closed enum.

Covers:

  §A   Closed taxonomies — value counts + frozen vocabularies
  §B   Frozen dataclass round-trips (to_dict / from_dict)
  §C   Empty-hooks dispatch → ALL_OK with empty records
  §D   Single-hook happy path → OK record with positive duration
  §E   Single-hook exception → FAILED record with sanitized detail
  §F   Single-hook timeout → TIMED_OUT record + thread orphaned
  §G   Phase budget exhaustion → remaining hooks SKIPPED +
       budget_exhausted=True
  §H   Per-hook timeout < remaining budget → uses per-hook
  §I   Per-hook timeout > remaining budget → uses budget
       (the min() clamp pin)
  §J   Determinism — same hooks + same context → same outcome
       histogram (modulo timing fields)
  §K   Garbage hooks list (None, non-tuples, non-callables) —
       silently filtered + dispatcher returns clean result
  §L   Garbage budget values (negative, 0, NaN, garbage) collapse
       to safe defaults
  §M   Sync discipline — AST pin: no asyncio import in the
       module
  §N   No authority imports — AST pin: no orchestrator /
       iron_gate / yaml_writer imports
  §O   hooks_by_outcome() shape includes all 4 outcomes
"""
from __future__ import annotations

import ast
import inspect
import json
import time
from typing import List
from unittest import mock

import pytest

from backend.core.ouroboros.battle_test.termination_hook import (
    DEFAULT_HARD_EXIT_PHASE_BUDGET_S,
    DEFAULT_PER_HOOK_TIMEOUT_S,
    DEFAULT_PHASE_BUDGET_S,
    HookExecutionRecord,
    HookOutcome,
    TERMINATION_HOOK_SCHEMA_VERSION,
    TerminationCause,
    TerminationDispatchResult,
    TerminationHookContext,
    TerminationPhase,
    dispatch_phase_sync,
)


# ---------------------------------------------------------------------------
# Fixtures + builders
# ---------------------------------------------------------------------------


def _ctx(
    *,
    cause: TerminationCause = TerminationCause.WALL_CLOCK_CAP,
    phase: TerminationPhase = (
        TerminationPhase.PRE_SHUTDOWN_EVENT_SET
    ),
    session_dir: str = "/tmp/test-session",
    started_at: float = 1000.0,
    stop_reason: str = "wall_clock_cap",
) -> TerminationHookContext:
    return TerminationHookContext(
        cause=cause,
        phase=phase,
        session_dir=session_dir,
        started_at=started_at,
        stop_reason=stop_reason,
    )


# ---------------------------------------------------------------------------
# §A — Closed taxonomy invariants
# ---------------------------------------------------------------------------


class TestTaxonomy:
    def test_termination_cause_eight_values(self):
        assert len(list(TerminationCause)) == 8

    def test_termination_cause_vocabulary_frozen(self):
        # Pin the literal vocabulary against silent additions.
        expected = {
            "wall_clock_cap", "sigterm", "sigint", "sighup",
            "idle_timeout", "budget_exceeded", "normal_exit",
            "unknown",
        }
        assert {c.value for c in TerminationCause} == expected

    def test_termination_phase_three_values(self):
        assert len(list(TerminationPhase)) == 3
        assert {p.value for p in TerminationPhase} == {
            "pre_shutdown_event_set",
            "post_async_cleanup",
            "pre_hard_exit",
        }

    def test_hook_outcome_four_values(self):
        assert len(list(HookOutcome)) == 4
        assert {o.value for o in HookOutcome} == {
            "ok", "failed", "timed_out", "skipped",
        }

    def test_default_budgets_documented(self):
        assert DEFAULT_PER_HOOK_TIMEOUT_S == 5.0
        assert DEFAULT_PHASE_BUDGET_S == 10.0
        assert DEFAULT_HARD_EXIT_PHASE_BUDGET_S == 2.0
        # Hard-exit phase MUST be tighter than the regular phase.
        assert (
            DEFAULT_HARD_EXIT_PHASE_BUDGET_S
            < DEFAULT_PHASE_BUDGET_S
        )

    def test_schema_version_pin(self):
        assert (
            TERMINATION_HOOK_SCHEMA_VERSION == "termination_hook.v1"
        )


# ---------------------------------------------------------------------------
# §B — Frozen dataclass round-trips
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_context_to_dict_shape(self):
        c = _ctx()
        d = c.to_dict()
        assert d["cause"] == "wall_clock_cap"
        assert d["phase"] == "pre_shutdown_event_set"
        assert d["session_dir"] == "/tmp/test-session"
        assert d["started_at"] == 1000.0
        assert d["stop_reason"] == "wall_clock_cap"
        assert d["schema_version"] == TERMINATION_HOOK_SCHEMA_VERSION

    def test_record_round_trip(self):
        r = HookExecutionRecord(
            hook_name="test_hook",
            outcome=HookOutcome.OK,
            duration_ms=42.5,
            detail="all good",
        )
        d = r.to_dict()
        r2 = HookExecutionRecord.from_dict(d)
        assert r2 is not None
        assert r2.hook_name == "test_hook"
        assert r2.outcome is HookOutcome.OK
        assert r2.duration_ms == 42.5
        assert r2.detail == "all good"

    def test_record_schema_mismatch_returns_none(self):
        d = {
            "schema_version": "wrong.v9",
            "hook_name": "x",
            "outcome": "ok",
            "duration_ms": 1.0,
        }
        assert HookExecutionRecord.from_dict(d) is None

    def test_dispatch_result_to_dict_shape(self):
        r = TerminationDispatchResult(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            records=(),
            total_duration_ms=0.0,
            budget_exhausted=False,
        )
        d = r.to_dict()
        # Round-trip through json.dumps to verify
        # serializability — Slice 4's SSE event payload uses this.
        assert isinstance(json.dumps(d), str)
        assert "outcome_histogram" in d
        # All 4 outcomes present in the histogram, even at zero.
        assert set(d["outcome_histogram"].keys()) == {
            "ok", "failed", "timed_out", "skipped",
        }


# ---------------------------------------------------------------------------
# §C – §F — Single-hook dispatch matrix
# ---------------------------------------------------------------------------


class TestDispatchMatrix:
    def test_empty_hooks_yields_clean_result(self):
        result = dispatch_phase_sync(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            hooks=[],
            context=_ctx(),
        )
        assert result.records == ()
        assert result.budget_exhausted is False
        assert result.all_ok() is True
        assert result.total_duration_ms >= 0.0

    def test_single_hook_happy_path(self):
        captured: List[TerminationHookContext] = []

        def my_hook(ctx):
            captured.append(ctx)

        result = dispatch_phase_sync(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            hooks=[("partial_summary_writer", my_hook)],
            context=_ctx(),
        )
        assert len(captured) == 1
        assert len(result.records) == 1
        rec = result.records[0]
        assert rec.hook_name == "partial_summary_writer"
        assert rec.outcome is HookOutcome.OK
        assert rec.duration_ms >= 0.0

    def test_hook_exception_recorded_as_failed(self):
        def boom(ctx):
            raise RuntimeError("disk full")

        result = dispatch_phase_sync(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            hooks=[("boom", boom)],
            context=_ctx(),
        )
        assert len(result.records) == 1
        rec = result.records[0]
        assert rec.outcome is HookOutcome.FAILED
        assert "RuntimeError" in rec.detail
        assert "disk full" in rec.detail
        assert result.all_ok() is False

    def test_hook_timeout_recorded_as_timed_out(self):
        # Slow hook (200ms) with very tight per-hook timeout (50ms).
        def slow(ctx):
            time.sleep(0.2)

        result = dispatch_phase_sync(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            hooks=[("slow", slow)],
            context=_ctx(),
            per_hook_timeout_s=0.05,
            phase_budget_s=10.0,
        )
        assert len(result.records) == 1
        rec = result.records[0]
        assert rec.outcome is HookOutcome.TIMED_OUT
        assert "timed_out_after" in rec.detail
        assert "thread_orphaned" in rec.detail

    def test_baseexception_caught_not_propagated(self):
        # Defense in depth — KeyboardInterrupt etc. must not
        # escape the worker.
        def panic(ctx):
            raise KeyboardInterrupt("user pressed ctrl-c")

        result = dispatch_phase_sync(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.SIGTERM,
            hooks=[("panic", panic)],
            context=_ctx(),
        )
        assert len(result.records) == 1
        assert result.records[0].outcome is HookOutcome.FAILED
        assert "KeyboardInterrupt" in result.records[0].detail


# ---------------------------------------------------------------------------
# §G — Phase budget exhaustion
# ---------------------------------------------------------------------------


class TestBudgetEnforcement:
    def test_phase_budget_exhausts_remaining_hooks_skipped(self):
        # 3 hooks: first sleeps for the entire budget; remaining
        # 2 must be SKIPPED (not just timed out — never started).
        executed: List[str] = []

        def slow(ctx):
            time.sleep(0.15)
            executed.append("slow")

        def quick_a(ctx):
            executed.append("quick_a")

        def quick_b(ctx):
            executed.append("quick_b")

        result = dispatch_phase_sync(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            hooks=[
                ("slow", slow),
                ("quick_a", quick_a),
                ("quick_b", quick_b),
            ],
            context=_ctx(),
            per_hook_timeout_s=0.5,
            phase_budget_s=0.1,  # tighter than slow's 0.15s sleep
        )
        # slow times out (per-hook clamped to remaining budget);
        # then budget is exhausted; quick_a + quick_b SKIPPED.
        assert len(result.records) == 3
        # First hook: TIMED_OUT (effective timeout was the
        # remaining budget which is < 0.15s).
        assert result.records[0].outcome is HookOutcome.TIMED_OUT
        assert result.records[1].outcome is HookOutcome.SKIPPED
        assert result.records[2].outcome is HookOutcome.SKIPPED
        # quick_a + quick_b never executed.
        assert "quick_a" not in executed
        assert "quick_b" not in executed
        assert result.budget_exhausted is True

    def test_per_hook_timeout_smaller_than_budget_used(self):
        # per_hook=0.05s, budget=10.0s. The min() clamp picks
        # per_hook because it's smaller.
        def slow(ctx):
            time.sleep(0.5)

        start = time.monotonic()
        result = dispatch_phase_sync(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            hooks=[("slow", slow)],
            context=_ctx(),
            per_hook_timeout_s=0.05,
            phase_budget_s=10.0,
        )
        elapsed = time.monotonic() - start
        assert result.records[0].outcome is HookOutcome.TIMED_OUT
        # Should have returned in ~0.05s, not ~0.5s — the clamp
        # held.
        assert elapsed < 0.4

    def test_per_hook_timeout_larger_than_budget_clamped(self):
        # per_hook=10.0s, budget=0.05s. The clamp picks budget.
        def slow(ctx):
            time.sleep(0.5)

        start = time.monotonic()
        result = dispatch_phase_sync(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            hooks=[("slow", slow)],
            context=_ctx(),
            per_hook_timeout_s=10.0,
            phase_budget_s=0.05,
        )
        elapsed = time.monotonic() - start
        assert result.records[0].outcome is HookOutcome.TIMED_OUT
        assert elapsed < 0.4
        assert result.budget_exhausted is False  # only 1 hook
                                                  # (it timed out
                                                  # but no remaining
                                                  # hooks to skip)

    def test_phase_budget_runs_quick_hooks_under_cap(self):
        # 5 quick hooks that fit comfortably in a 1s budget.
        counters: List[int] = [0]

        def quick(ctx):
            counters[0] += 1

        result = dispatch_phase_sync(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.NORMAL_EXIT,
            hooks=[(f"q{i}", quick) for i in range(5)],
            context=_ctx(),
            per_hook_timeout_s=1.0,
            phase_budget_s=1.0,
        )
        assert counters[0] == 5
        assert all(
            r.outcome is HookOutcome.OK for r in result.records
        )
        assert result.budget_exhausted is False


# ---------------------------------------------------------------------------
# §J — Determinism (modulo timing-derived fields)
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_outcome_histogram_stable_across_runs(self):
        def ok(ctx):
            pass

        def fail(ctx):
            raise ValueError("nope")

        hooks = [("a", ok), ("b", fail), ("c", ok)]
        r1 = dispatch_phase_sync(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            hooks=hooks,
            context=_ctx(),
        )
        r2 = dispatch_phase_sync(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            hooks=hooks,
            context=_ctx(),
        )
        h1 = r1.hooks_by_outcome()
        h2 = r2.hooks_by_outcome()
        assert h1 == h2
        assert h1[HookOutcome.OK] == 2
        assert h1[HookOutcome.FAILED] == 1


# ---------------------------------------------------------------------------
# §K — Garbage inputs
# ---------------------------------------------------------------------------


class TestGarbageInputs:
    def test_non_iterable_hooks_handled(self):
        # Not iterable — dispatcher should treat as empty and not
        # raise.
        result = dispatch_phase_sync(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.UNKNOWN,
            hooks=42,  # type: ignore[arg-type]
            context=_ctx(),
        )
        assert result.records == ()

    def test_garbage_entries_filtered(self):
        def good(ctx):
            pass

        result = dispatch_phase_sync(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.UNKNOWN,
            hooks=[
                None,                     # not a tuple
                "not a tuple",            # str unpack to chars
                ("a",),                   # 1-tuple
                ("name", None),           # not callable
                ("name", 42),             # int not callable
                ("good", good),           # the only valid one
            ],  # type: ignore[list-item]
            context=_ctx(),
        )
        assert len(result.records) == 1
        assert result.records[0].hook_name == "good"
        assert result.records[0].outcome is HookOutcome.OK

    def test_unnamed_hook_named_unnamed(self):
        def fn(ctx):
            pass
        result = dispatch_phase_sync(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.UNKNOWN,
            hooks=[("", fn)],
            context=_ctx(),
        )
        assert result.records[0].hook_name == "unnamed"

    def test_garbage_budgets_collapse_to_defaults(self):
        def quick(ctx):
            pass

        for bad_phase, bad_per_hook in [
            (-1.0, -1.0),
            (0.0, 0.0),
            ("garbage", "garbage"),
            (None, None),
        ]:
            result = dispatch_phase_sync(
                phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
                cause=TerminationCause.UNKNOWN,
                hooks=[("q", quick)],
                context=_ctx(),
                per_hook_timeout_s=bad_per_hook,  # type: ignore[arg-type]
                phase_budget_s=bad_phase,  # type: ignore[arg-type]
            )
            # Should still execute the hook successfully (defaults
            # provide ample room for a no-op).
            assert result.records[0].outcome is HookOutcome.OK


# ---------------------------------------------------------------------------
# §M – §N — AST authority pins
# ---------------------------------------------------------------------------


class TestAuthorityPins:
    def test_module_does_not_import_asyncio(self):
        # STRICT sync-first directive: the substrate MUST NOT
        # touch asyncio at all. Hooks for PRE_SHUTDOWN_EVENT_SET
        # need to survive a wedged asyncio loop.
        from backend.core.ouroboros.battle_test import (
            termination_hook,
        )
        src = inspect.getsource(termination_hook)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "asyncio" not in node.module.split("."), (
                    f"forbidden asyncio import: {node.module}"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert (
                        "asyncio" not in alias.name.split(".")
                    ), (
                        f"forbidden asyncio import: {alias.name}"
                    )

    def test_module_does_not_import_authority_modules(self):
        from backend.core.ouroboros.battle_test import (
            termination_hook,
        )
        src = inspect.getsource(termination_hook)
        tree = ast.parse(src)
        forbidden = {
            "yaml_writer", "orchestrator", "iron_gate",
            "risk_tier", "change_engine",
            "candidate_generator", "gate", "policy",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                for f in forbidden:
                    assert f not in parts, (
                        f"forbidden import: {node.module}"
                    )

    def test_module_imports_only_stdlib(self):
        # Slice 1 ships PURE-stdlib substrate. Slice 2's registry
        # is the FIRST thing to import this module; the substrate
        # itself imports no backend.* modules.
        from backend.core.ouroboros.battle_test import (
            termination_hook,
        )
        src = inspect.getsource(termination_hook)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("backend."), (
                    f"non-stdlib import: {node.module}"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("backend."), (
                        f"non-stdlib import: {alias.name}"
                    )


# ---------------------------------------------------------------------------
# §O — hooks_by_outcome shape
# ---------------------------------------------------------------------------


class TestOutcomeHistogram:
    def test_histogram_has_all_four_outcomes(self):
        result = TerminationDispatchResult(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.UNKNOWN,
            records=(),
            total_duration_ms=0.0,
            budget_exhausted=False,
        )
        h = result.hooks_by_outcome()
        assert set(h.keys()) == set(HookOutcome)
        for v in h.values():
            assert v == 0

    def test_histogram_counts_correct(self):
        records = (
            HookExecutionRecord(
                hook_name="a", outcome=HookOutcome.OK,
                duration_ms=1.0,
            ),
            HookExecutionRecord(
                hook_name="b", outcome=HookOutcome.OK,
                duration_ms=2.0,
            ),
            HookExecutionRecord(
                hook_name="c", outcome=HookOutcome.FAILED,
                duration_ms=3.0,
            ),
            HookExecutionRecord(
                hook_name="d", outcome=HookOutcome.TIMED_OUT,
                duration_ms=5000.0,
            ),
            HookExecutionRecord(
                hook_name="e", outcome=HookOutcome.SKIPPED,
                duration_ms=0.0,
            ),
        )
        result = TerminationDispatchResult(
            phase=TerminationPhase.POST_ASYNC_CLEANUP,
            cause=TerminationCause.NORMAL_EXIT,
            records=records,
            total_duration_ms=5006.0,
            budget_exhausted=False,
        )
        h = result.hooks_by_outcome()
        assert h[HookOutcome.OK] == 2
        assert h[HookOutcome.FAILED] == 1
        assert h[HookOutcome.TIMED_OUT] == 1
        assert h[HookOutcome.SKIPPED] == 1


# ---------------------------------------------------------------------------
# Sanity — context propagation
# ---------------------------------------------------------------------------


def test_context_propagated_to_hook():
    received: List[TerminationHookContext] = []

    def capture(ctx):
        received.append(ctx)

    custom_ctx = _ctx(
        cause=TerminationCause.SIGHUP,
        phase=TerminationPhase.POST_ASYNC_CLEANUP,
        session_dir="/var/run/sess-xyz",
        started_at=2026.0,
        stop_reason="parent_died",
    )
    dispatch_phase_sync(
        phase=TerminationPhase.POST_ASYNC_CLEANUP,
        cause=TerminationCause.SIGHUP,
        hooks=[("c", capture)],
        context=custom_ctx,
    )
    assert len(received) == 1
    got = received[0]
    assert got.cause is TerminationCause.SIGHUP
    assert got.session_dir == "/var/run/sess-xyz"
    assert got.stop_reason == "parent_died"
