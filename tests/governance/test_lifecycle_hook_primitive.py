"""Lifecycle Hook Registry Slice 1 — primitive regression spine.

Covers:
  * Closed 5-value LifecycleEvent + HookOutcome taxonomies
    (J.A.R.M.A.T.R.I.X.)
  * Frozen dataclass mutation guards + to_dict/from_dict round-trip
  * Total compute_hook_decision aggregation — BLOCK-wins semantics
    over every (event × results) combination
  * Phase C MonotonicTighteningVerdict.PASSED stamping outcome-aware
  * make_hook_result auto-stamps tightening per outcome
  * Master flag asymmetric env semantics
  * Env-knob clamping (max-per-event, default-timeout)
  * AST-walked authority invariants (pure-stdlib, no async, no
    exec/eval/compile)
"""
from __future__ import annotations

import ast
import pathlib
from dataclasses import FrozenInstanceError

import pytest

from backend.core.ouroboros.governance.lifecycle_hook import (
    AggregateHookDecision,
    HookContext,
    HookOutcome,
    HookResult,
    LIFECYCLE_HOOK_SCHEMA_VERSION,
    LifecycleEvent,
    compute_hook_decision,
    default_hook_timeout_s,
    lifecycle_hooks_enabled,
    make_hook_result,
    max_hooks_per_event,
)


# ---------------------------------------------------------------------------
# Closed-taxonomy invariants
# ---------------------------------------------------------------------------


class TestClosedTaxonomies:
    def test_event_has_exactly_five_values(self):
        assert len(list(LifecycleEvent)) == 5

    def test_event_value_set_exact(self):
        expected = {
            "pre_generate", "pre_apply", "post_apply",
            "post_verify", "on_operator_action",
        }
        actual = {v.value for v in LifecycleEvent}
        assert actual == expected

    def test_outcome_has_exactly_five_values(self):
        assert len(list(HookOutcome)) == 5

    def test_outcome_value_set_exact(self):
        expected = {
            "continue", "block", "warn", "disabled", "failed",
        }
        actual = {v.value for v in HookOutcome}
        assert actual == expected

    def test_event_is_str_enum(self):
        for v in LifecycleEvent:
            assert isinstance(v.value, str)
            assert isinstance(v, str)

    def test_outcome_is_str_enum(self):
        for v in HookOutcome:
            assert isinstance(v.value, str)
            assert isinstance(v, str)


# ---------------------------------------------------------------------------
# Frozen dataclass guards
# ---------------------------------------------------------------------------


class TestFrozenContext:
    def test_context_is_frozen(self):
        c = HookContext(event=LifecycleEvent.PRE_APPLY)
        with pytest.raises(FrozenInstanceError):
            c.event = LifecycleEvent.POST_APPLY  # type: ignore[misc]

    def test_context_default_schema_version(self):
        c = HookContext(event=LifecycleEvent.PRE_GENERATE)
        assert c.schema_version == LIFECYCLE_HOOK_SCHEMA_VERSION
        assert c.schema_version == "lifecycle_hook.1"

    def test_context_default_payload_is_empty_dict(self):
        c = HookContext(event=LifecycleEvent.PRE_APPLY)
        assert c.payload == {}

    def test_context_to_dict_round_trip(self):
        c = HookContext(
            event=LifecycleEvent.POST_APPLY,
            op_id="op-x", phase="APPLY",
            payload={"target_paths": ["a.py"], "diff_size": 42},
            started_ts=12345.6,
        )
        c2 = HookContext.from_dict(c.to_dict())
        assert c2.event is c.event
        assert c2.op_id == c.op_id
        assert c2.phase == c.phase
        assert c2.payload == c.payload
        assert c2.started_ts == c.started_ts

    def test_context_from_dict_unknown_event_degrades(self):
        c = HookContext.from_dict({"event": "not-a-real-event"})
        # Defensive coerce to PRE_APPLY (most common boundary).
        assert c.event is LifecycleEvent.PRE_APPLY

    def test_context_from_dict_garbage_payload_degrades(self):
        c = HookContext.from_dict({
            "event": "pre_apply",
            "payload": "not-a-mapping",
        })
        assert c.payload == {}

    def test_context_from_dict_never_raises(self):
        for bad in [{}, None, {"event": object()}]:
            try:
                c = HookContext.from_dict(bad or {})
                assert isinstance(c, HookContext)
            except Exception:
                pytest.fail(f"from_dict raised on {bad!r}")


class TestFrozenResult:
    def test_result_is_frozen(self):
        r = HookResult(hook_name="x", outcome=HookOutcome.CONTINUE)
        with pytest.raises(FrozenInstanceError):
            r.outcome = HookOutcome.BLOCK  # type: ignore[misc]

    def test_result_to_dict_round_trip(self):
        r = HookResult(
            hook_name="my-hook",
            outcome=HookOutcome.BLOCK,
            detail="touches secrets",
            elapsed_ms=12.5,
            monotonic_tightening_verdict="passed",
        )
        r2 = HookResult.from_dict(r.to_dict())
        assert r2 == r

    def test_result_from_dict_unknown_outcome_degrades(self):
        r = HookResult.from_dict({
            "hook_name": "x", "outcome": "not-real",
        })
        assert r.outcome is HookOutcome.FAILED

    def test_result_from_dict_never_raises(self):
        for bad in [{}, None, {"outcome": None}, {"outcome": object()}]:
            try:
                r = HookResult.from_dict(bad or {})
                assert isinstance(r, HookResult)
            except Exception:
                pytest.fail(f"from_dict raised on {bad!r}")

    def test_is_blocking_only_true_for_block(self):
        for outcome in HookOutcome:
            r = HookResult(hook_name="x", outcome=outcome)
            assert r.is_blocking == (outcome is HookOutcome.BLOCK)

    def test_is_active_for_block_and_warn_only(self):
        active = {HookOutcome.BLOCK, HookOutcome.WARN}
        for outcome in HookOutcome:
            r = HookResult(hook_name="x", outcome=outcome)
            assert r.is_active == (outcome in active)

    def test_is_tightening_only_for_block(self):
        for outcome in HookOutcome:
            r = HookResult(hook_name="x", outcome=outcome)
            assert r.is_tightening == (outcome is HookOutcome.BLOCK)


# ---------------------------------------------------------------------------
# Aggregation matrix — BLOCK-wins semantics
# ---------------------------------------------------------------------------


class TestAggregationMatrix:
    def _r(self, name: str, outcome: HookOutcome) -> HookResult:
        return HookResult(hook_name=name, outcome=outcome)

    def test_empty_results_yield_continue(self):
        agg = compute_hook_decision(
            LifecycleEvent.PRE_APPLY, (),
        )
        assert agg.aggregate is HookOutcome.CONTINUE
        assert agg.total_hooks == 0
        assert agg.blocking_hooks == ()
        assert agg.warning_hooks == ()
        assert agg.failed_hooks == ()
        assert agg.monotonic_tightening_verdict == ""

    def test_all_continue_yields_continue(self):
        agg = compute_hook_decision(
            LifecycleEvent.PRE_APPLY,
            (
                self._r("h1", HookOutcome.CONTINUE),
                self._r("h2", HookOutcome.CONTINUE),
            ),
        )
        assert agg.aggregate is HookOutcome.CONTINUE
        assert agg.total_hooks == 2

    def test_any_block_yields_block(self):
        agg = compute_hook_decision(
            LifecycleEvent.PRE_APPLY,
            (
                self._r("h1", HookOutcome.CONTINUE),
                self._r("h2", HookOutcome.BLOCK),
                self._r("h3", HookOutcome.CONTINUE),
            ),
        )
        assert agg.aggregate is HookOutcome.BLOCK
        assert agg.blocking_hooks == ("h2",)
        assert agg.monotonic_tightening_verdict == "passed"

    def test_block_dominates_warn(self):
        """If both BLOCK and WARN appear, aggregate is BLOCK."""
        agg = compute_hook_decision(
            LifecycleEvent.PRE_APPLY,
            (
                self._r("h1", HookOutcome.WARN),
                self._r("h2", HookOutcome.BLOCK),
            ),
        )
        assert agg.aggregate is HookOutcome.BLOCK
        assert agg.blocking_hooks == ("h2",)
        # warning_hooks still records the WARN for audit.
        assert agg.warning_hooks == ("h1",)

    def test_warn_only_yields_warn(self):
        agg = compute_hook_decision(
            LifecycleEvent.POST_APPLY,
            (
                self._r("h1", HookOutcome.CONTINUE),
                self._r("h2", HookOutcome.WARN),
            ),
        )
        assert agg.aggregate is HookOutcome.WARN
        assert agg.warning_hooks == ("h2",)
        assert agg.monotonic_tightening_verdict == ""

    def test_failed_only_yields_continue(self):
        """Buggy hooks (FAILED) cannot stop the orchestrator —
        non-blocking by design."""
        agg = compute_hook_decision(
            LifecycleEvent.PRE_APPLY,
            (
                self._r("h1", HookOutcome.FAILED),
                self._r("h2", HookOutcome.FAILED),
            ),
        )
        assert agg.aggregate is HookOutcome.CONTINUE
        assert agg.failed_hooks == ("h1", "h2")
        assert agg.blocking_hooks == ()

    def test_disabled_only_yields_continue(self):
        agg = compute_hook_decision(
            LifecycleEvent.POST_VERIFY,
            (
                self._r("h1", HookOutcome.DISABLED),
                self._r("h2", HookOutcome.DISABLED),
            ),
        )
        assert agg.aggregate is HookOutcome.CONTINUE

    def test_multiple_blocks_all_recorded(self):
        agg = compute_hook_decision(
            LifecycleEvent.PRE_APPLY,
            (
                self._r("h1", HookOutcome.BLOCK),
                self._r("h2", HookOutcome.BLOCK),
                self._r("h3", HookOutcome.BLOCK),
            ),
        )
        assert agg.aggregate is HookOutcome.BLOCK
        assert agg.blocking_hooks == ("h1", "h2", "h3")

    def test_aggregate_propagates_event(self):
        for ev in LifecycleEvent:
            agg = compute_hook_decision(ev, ())
            assert agg.event is ev


class TestAggregationDefensive:
    def test_aggregate_garbage_event_coerces_to_pre_apply(self):
        agg = compute_hook_decision(
            "not-an-event",  # type: ignore[arg-type]
            (),
        )
        assert agg.event is LifecycleEvent.PRE_APPLY

    def test_aggregate_garbage_results_drops_non_HookResult(self):
        agg = compute_hook_decision(
            LifecycleEvent.PRE_APPLY,
            (
                "not-a-result",  # type: ignore[arg-type]
                HookResult(hook_name="real", outcome=HookOutcome.BLOCK),
                42,  # type: ignore[arg-type]
            ),
        )
        # Only the real one counts.
        assert agg.total_hooks == 1
        assert agg.aggregate is HookOutcome.BLOCK
        assert agg.blocking_hooks == ("real",)

    def test_aggregate_none_results_treated_as_empty(self):
        agg = compute_hook_decision(
            LifecycleEvent.PRE_APPLY, None,  # type: ignore[arg-type]
        )
        assert agg.total_hooks == 0
        assert agg.aggregate is HookOutcome.CONTINUE

    def test_aggregate_never_raises_on_garbage(self):
        garbage = [
            (LifecycleEvent.PRE_APPLY, "not-a-tuple"),
            ("not-event", "not-a-tuple"),
            (None, None),
        ]
        for ev, results in garbage:
            try:
                agg = compute_hook_decision(
                    ev,  # type: ignore[arg-type]
                    results,  # type: ignore[arg-type]
                )
                assert isinstance(agg, AggregateHookDecision)
            except Exception:
                pytest.fail(f"aggregate raised on {ev!r}, {results!r}")


# ---------------------------------------------------------------------------
# Phase C tightening stamping
# ---------------------------------------------------------------------------


class TestPhaseCTightening:
    def test_block_aggregate_stamps_passed(self):
        agg = compute_hook_decision(
            LifecycleEvent.PRE_APPLY,
            (HookResult(hook_name="x", outcome=HookOutcome.BLOCK),),
        )
        assert agg.monotonic_tightening_verdict == "passed"
        assert agg.is_tightening is True

    @pytest.mark.parametrize("outcome", [
        HookOutcome.WARN, HookOutcome.CONTINUE,
        HookOutcome.DISABLED, HookOutcome.FAILED,
    ])
    def test_non_block_aggregates_stamp_empty(
        self, outcome: HookOutcome,
    ):
        agg = compute_hook_decision(
            LifecycleEvent.PRE_APPLY,
            (HookResult(hook_name="x", outcome=outcome),),
        )
        assert agg.monotonic_tightening_verdict == ""
        assert agg.is_tightening is False


# ---------------------------------------------------------------------------
# make_hook_result auto-stamping
# ---------------------------------------------------------------------------


class TestMakeHookResult:
    def test_block_auto_stamps_passed(self):
        r = make_hook_result("x", HookOutcome.BLOCK)
        assert r.monotonic_tightening_verdict == "passed"

    @pytest.mark.parametrize("outcome", [
        HookOutcome.WARN, HookOutcome.CONTINUE,
        HookOutcome.DISABLED, HookOutcome.FAILED,
    ])
    def test_non_block_auto_stamps_empty(self, outcome: HookOutcome):
        r = make_hook_result("x", outcome)
        assert r.monotonic_tightening_verdict == ""

    def test_truncates_hook_name(self):
        r = make_hook_result("x" * 500, HookOutcome.CONTINUE)
        assert len(r.hook_name) == 128

    def test_truncates_detail(self):
        r = make_hook_result(
            "x", HookOutcome.WARN, detail="x" * 5000,
        )
        assert len(r.detail) == 1000

    def test_clamps_negative_elapsed(self):
        r = make_hook_result(
            "x", HookOutcome.CONTINUE, elapsed_ms=-5.0,
        )
        assert r.elapsed_ms == 0.0

    def test_garbage_outcome_degrades_to_failed(self):
        r = make_hook_result("x", "not-an-outcome")  # type: ignore[arg-type]
        assert r.outcome is HookOutcome.FAILED

    def test_never_raises(self):
        for bad in [None, object(), 42]:
            try:
                r = make_hook_result(
                    "x", bad,  # type: ignore[arg-type]
                )
                assert isinstance(r, HookResult)
            except Exception:
                pytest.fail(f"make_hook_result raised on {bad!r}")


# ---------------------------------------------------------------------------
# Master flag asymmetric env semantics
# ---------------------------------------------------------------------------


class TestMasterFlagSemantics:
    def test_default_is_false_pre_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_LIFECYCLE_HOOKS_ENABLED", raising=False,
        )
        assert lifecycle_hooks_enabled() is False

    def test_empty_string_is_default_false(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LIFECYCLE_HOOKS_ENABLED", "")
        assert lifecycle_hooks_enabled() is False

    def test_whitespace_is_default_false(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LIFECYCLE_HOOKS_ENABLED", "   ")
        assert lifecycle_hooks_enabled() is False

    @pytest.mark.parametrize(
        "truthy", ["1", "true", "yes", "on", "TRUE"],
    )
    def test_truthy_enables(self, monkeypatch, truthy: str):
        monkeypatch.setenv("JARVIS_LIFECYCLE_HOOKS_ENABLED", truthy)
        assert lifecycle_hooks_enabled() is True

    @pytest.mark.parametrize(
        "falsy", ["0", "false", "no", "off", "FALSE"],
    )
    def test_falsy_disables(self, monkeypatch, falsy: str):
        monkeypatch.setenv("JARVIS_LIFECYCLE_HOOKS_ENABLED", falsy)
        assert lifecycle_hooks_enabled() is False


# ---------------------------------------------------------------------------
# Env-knob clamping
# ---------------------------------------------------------------------------


class TestEnvKnobClamping:
    def test_max_hooks_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_LIFECYCLE_HOOKS_MAX_PER_EVENT", raising=False,
        )
        assert max_hooks_per_event() == 16

    def test_max_hooks_floor_and_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_LIFECYCLE_HOOKS_MAX_PER_EVENT", "0",
        )
        assert max_hooks_per_event() == 1
        monkeypatch.setenv(
            "JARVIS_LIFECYCLE_HOOKS_MAX_PER_EVENT", "9999",
        )
        assert max_hooks_per_event() == 256

    def test_max_hooks_garbage_uses_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_LIFECYCLE_HOOKS_MAX_PER_EVENT", "not-a-number",
        )
        assert max_hooks_per_event() == 16

    def test_default_timeout_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_LIFECYCLE_HOOKS_DEFAULT_TIMEOUT_S", raising=False,
        )
        assert default_hook_timeout_s() == 5.0

    def test_default_timeout_floor_and_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_LIFECYCLE_HOOKS_DEFAULT_TIMEOUT_S", "0.001",
        )
        assert default_hook_timeout_s() == 0.1
        monkeypatch.setenv(
            "JARVIS_LIFECYCLE_HOOKS_DEFAULT_TIMEOUT_S", "9999",
        )
        assert default_hook_timeout_s() == 60.0


# ---------------------------------------------------------------------------
# Authority invariant — pure-stdlib at hot path
# ---------------------------------------------------------------------------


class TestPureStdlibInvariant:
    def _source(self) -> str:
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "lifecycle_hook.py"
        )
        return path.read_text()

    def test_no_governance_imports_at_module_top(self):
        """Slice 1 stays pure-stdlib (registration-contract
        exemption applies — n/a here since Slice 5 adds the
        register_* functions)."""
        source = self._source()
        tree = ast.parse(source)
        registration_funcs = {
            "register_flags", "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in ast.walk(tree):
            if isinstance(fnode, ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "backend." in module or "governance" in module:
                    lineno = getattr(node, "lineno", 0)
                    if any(s <= lineno <= e for s, e in exempt_ranges):
                        continue
                    raise AssertionError(
                        f"Slice 1 must be pure-stdlib — found "
                        f"governance import {module!r} at line {lineno}"
                    )

    def test_no_async_def_in_module(self):
        source = self._source()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                raise AssertionError(
                    f"Slice 1 must be sync — found async def "
                    f"{node.name!r} at line "
                    f"{getattr(node, 'lineno', '?')}"
                )

    def test_no_exec_eval_compile_calls(self):
        source = self._source()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        raise AssertionError(
                            f"Slice 1 must NOT exec/eval/compile — "
                            f"found {node.func.id}() at line "
                            f"{getattr(node, 'lineno', '?')}"
                        )


# ---------------------------------------------------------------------------
# Schema version sanity
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_schema_version_constant(self):
        assert LIFECYCLE_HOOK_SCHEMA_VERSION == "lifecycle_hook.1"

    def test_schema_version_default_on_context(self):
        c = HookContext(event=LifecycleEvent.PRE_APPLY)
        assert c.schema_version == "lifecycle_hook.1"

    def test_schema_version_default_on_result(self):
        r = HookResult(hook_name="x", outcome=HookOutcome.CONTINUE)
        assert r.schema_version == "lifecycle_hook.1"

    def test_schema_version_default_on_aggregate(self):
        agg = compute_hook_decision(LifecycleEvent.PRE_APPLY, ())
        assert agg.schema_version == "lifecycle_hook.1"
