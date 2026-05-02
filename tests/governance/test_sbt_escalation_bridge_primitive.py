"""SBT-Probe Escalation Bridge Slice 1 — primitive regression spine.

Covers:
  * Closed 5-value EscalationDecision taxonomy (J.A.R.M.A.T.R.I.X.)
  * Frozen dataclass mutation guards + to_dict/from_dict round-trip
  * Total decision function — every (probe outcome × budget × flag)
    combination maps to expected EscalationDecision; NEVER raises
  * Phase C MonotonicTighteningVerdict.PASSED stamping outcome-aware
  * 5→3 TreeVerdict → ConfidenceCollapseAction mapping (every
    TreeVerdict value + garbage degrades to INCONCLUSIVE)
  * Master flag asymmetric env semantics (default false; explicit
    truthy/falsy overrides)
  * Env-knob clamping (cost cap, time cap)
  * Byte-parity to live ProbeOutcome / TreeVerdict /
    ConfidenceCollapseAction enums (string-constant pin)
  * AST-walked authority invariants — pure-stdlib at hot path
    (registration-contract exemption applies; n/a here since no
    register_* yet — Slice 3 adds them)
"""
from __future__ import annotations

import ast
import pathlib
from dataclasses import FrozenInstanceError

import pytest

from backend.core.ouroboros.governance.verification.sbt_escalation_bridge import (
    EscalationContext,
    EscalationDecision,
    EscalationVerdict,
    SBT_ESCALATION_BRIDGE_SCHEMA_VERSION,
    compute_escalation_decision,
    max_escalation_cost_usd,
    max_escalation_time_s,
    sbt_escalation_enabled,
    tree_verdict_to_collapse_action,
)


# ---------------------------------------------------------------------------
# Closed-taxonomy invariants
# ---------------------------------------------------------------------------


class TestClosedTaxonomy:
    def test_decision_has_exactly_five_values(self):
        """Closed-taxonomy invariant. New values require explicit
        scope-doc + Slice 2 wrapper update."""
        assert len(list(EscalationDecision)) == 5

    def test_decision_value_set_exact(self):
        expected = {
            "escalate", "skip", "budget_exhausted", "disabled", "failed",
        }
        actual = {v.value for v in EscalationDecision}
        assert actual == expected

    def test_decision_str_enum(self):
        for v in EscalationDecision:
            assert isinstance(v.value, str)
            assert isinstance(v, str)


# ---------------------------------------------------------------------------
# Frozen dataclass guards
# ---------------------------------------------------------------------------


class TestFrozenContext:
    def test_context_is_frozen(self):
        c = EscalationContext(probe_outcome="exhausted")
        with pytest.raises(FrozenInstanceError):
            c.probe_outcome = "mutated"  # type: ignore[misc]

    def test_context_default_schema_version(self):
        c = EscalationContext(probe_outcome="exhausted")
        assert c.schema_version == SBT_ESCALATION_BRIDGE_SCHEMA_VERSION
        assert c.schema_version == "sbt_escalation_bridge.1"

    def test_context_to_dict_round_trip(self):
        c = EscalationContext(
            probe_outcome="exhausted",
            cost_so_far_usd=0.05,
            time_so_far_s=12.5,
            op_id="op-x", target="y.py:42",
        )
        c2 = EscalationContext.from_dict(c.to_dict())
        assert c2 == c

    def test_context_from_dict_tolerates_garbage(self):
        bad_inputs = [
            {},
            {"probe_outcome": None},
            {"cost_so_far_usd": "not-a-float"},
            {"time_so_far_s": object()},
        ]
        for bad in bad_inputs:
            c = EscalationContext.from_dict(bad)
            assert isinstance(c, EscalationContext)


class TestFrozenVerdict:
    def test_verdict_is_frozen(self):
        v = EscalationVerdict(decision=EscalationDecision.SKIP)
        with pytest.raises(FrozenInstanceError):
            v.decision = EscalationDecision.ESCALATE  # type: ignore[misc]

    def test_verdict_to_dict_round_trip(self):
        v = EscalationVerdict(
            decision=EscalationDecision.ESCALATE,
            op_id="op-x", target="y.py", detail="reason",
            monotonic_tightening_verdict="passed",
        )
        v2 = EscalationVerdict.from_dict(v.to_dict())
        assert v2 == v

    def test_verdict_from_dict_unknown_decision_degrades_to_failed(self):
        v = EscalationVerdict.from_dict({"decision": "not-a-real-thing"})
        assert v.decision is EscalationDecision.FAILED

    def test_verdict_from_dict_never_raises(self):
        for bad in [{}, {"decision": None}, {"decision": object()}]:
            v = EscalationVerdict.from_dict(bad)
            assert isinstance(v, EscalationVerdict)

    def test_is_escalating_only_true_for_escalate(self):
        for d in EscalationDecision:
            v = EscalationVerdict(decision=d)
            assert v.is_escalating == (d is EscalationDecision.ESCALATE)
            assert v.is_tightening == v.is_escalating


# ---------------------------------------------------------------------------
# Decision matrix — every (probe outcome × budget × flag) maps
# ---------------------------------------------------------------------------


class TestComputeDecisionMatrix:
    def test_master_off_returns_disabled(self):
        v = compute_escalation_decision(
            EscalationContext(probe_outcome="exhausted"),
            enabled=False,
        )
        assert v.decision is EscalationDecision.DISABLED
        assert v.monotonic_tightening_verdict == ""

    @pytest.mark.parametrize(
        "outcome",
        ["converged", "diverged", "disabled", "failed"],
    )
    def test_non_trigger_outcomes_skip(self, outcome: str):
        """Probe already produced a usable signal or defensive
        fall-through; no escalation."""
        v = compute_escalation_decision(
            EscalationContext(probe_outcome=outcome),
            enabled=True,
        )
        assert v.decision is EscalationDecision.SKIP
        assert v.monotonic_tightening_verdict == ""

    def test_exhausted_with_budget_ok_escalates(self):
        v = compute_escalation_decision(
            EscalationContext(
                probe_outcome="exhausted",
                cost_so_far_usd=0.01,
                time_so_far_s=5.0,
            ),
            enabled=True,
            max_cost_usd=1.0,
            max_time_s=120.0,
        )
        assert v.decision is EscalationDecision.ESCALATE
        assert v.monotonic_tightening_verdict == "passed"
        assert v.is_escalating is True
        assert v.is_tightening is True

    def test_exhausted_with_cost_exceeded_returns_budget_exhausted(self):
        v = compute_escalation_decision(
            EscalationContext(
                probe_outcome="exhausted",
                cost_so_far_usd=0.50,
            ),
            enabled=True,
            max_cost_usd=0.10,
        )
        assert v.decision is EscalationDecision.BUDGET_EXHAUSTED
        assert v.monotonic_tightening_verdict == ""
        assert "cost_so_far" in v.detail

    def test_exhausted_with_time_exceeded_returns_budget_exhausted(self):
        v = compute_escalation_decision(
            EscalationContext(
                probe_outcome="exhausted",
                time_so_far_s=200.0,
            ),
            enabled=True,
            max_time_s=60.0,
        )
        assert v.decision is EscalationDecision.BUDGET_EXHAUSTED
        assert v.monotonic_tightening_verdict == ""
        assert "time_so_far" in v.detail

    def test_non_string_probe_outcome_returns_failed(self):
        v = compute_escalation_decision(
            EscalationContext(probe_outcome=42),  # type: ignore[arg-type]
            enabled=True,
        )
        assert v.decision is EscalationDecision.FAILED

    def test_unknown_probe_outcome_skips(self):
        """Unknown outcome strings fall through to SKIP — defensive
        (treat as 'probe handled it'). FAILED reserved for non-string
        garbage where the type itself is broken."""
        v = compute_escalation_decision(
            EscalationContext(probe_outcome="some_new_outcome"),
            enabled=True,
        )
        assert v.decision is EscalationDecision.SKIP

    def test_empty_probe_outcome_skips(self):
        v = compute_escalation_decision(
            EscalationContext(probe_outcome=""),
            enabled=True,
        )
        assert v.decision is EscalationDecision.SKIP

    def test_compute_propagates_op_id_and_target(self):
        v = compute_escalation_decision(
            EscalationContext(
                probe_outcome="exhausted",
                op_id="op-prop", target="foo.py:1",
            ),
            enabled=True,
        )
        assert v.op_id == "op-prop"
        assert v.target == "foo.py:1"

    def test_compute_never_raises_on_garbage(self):
        garbage = [
            EscalationContext(
                probe_outcome="exhausted",
                cost_so_far_usd=float("nan"),
            ),
            EscalationContext(
                probe_outcome="exhausted",
                time_so_far_s=-1.0,
            ),
        ]
        for ctx in garbage:
            v = compute_escalation_decision(ctx, enabled=True)
            assert isinstance(v, EscalationVerdict)

    def test_negative_cost_clamped_to_zero(self):
        v = compute_escalation_decision(
            EscalationContext(
                probe_outcome="exhausted",
                cost_so_far_usd=-5.0,
            ),
            enabled=True,
            max_cost_usd=0.10,
        )
        # -5 clamped to 0; budget OK → ESCALATE.
        assert v.decision is EscalationDecision.ESCALATE


# ---------------------------------------------------------------------------
# 5→3 collapse mapping
# ---------------------------------------------------------------------------


class TestTreeVerdictToCollapseAction:
    @pytest.mark.parametrize(
        "tree_verdict, expected_action",
        [
            ("converged", "retry_with_feedback"),
            ("diverged", "escalate_to_operator"),
            ("inconclusive", "inconclusive"),
            ("truncated", "inconclusive"),
            ("failed", "inconclusive"),
        ],
    )
    def test_known_verdict_maps_to_expected_action(
        self, tree_verdict: str, expected_action: str,
    ):
        assert (
            tree_verdict_to_collapse_action(tree_verdict)
            == expected_action
        )

    @pytest.mark.parametrize(
        "garbage",
        [None, "", "   ", "CONVERGED-typo", "unknown_state", 42],
    )
    def test_garbage_degrades_to_inconclusive(self, garbage):
        assert (
            tree_verdict_to_collapse_action(garbage)
            == "inconclusive"
        )

    def test_mapping_never_raises(self):
        for g in [None, object(), [], {}, 1.5]:
            try:
                out = tree_verdict_to_collapse_action(g)
                assert isinstance(out, str)
            except Exception:
                pytest.fail(f"mapping raised on input {g!r}")


# ---------------------------------------------------------------------------
# Master flag asymmetric env semantics
# ---------------------------------------------------------------------------


class TestMasterFlagSemantics:
    def test_default_is_false_pre_graduation(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_ESCALATION_ENABLED", raising=False)
        assert sbt_escalation_enabled() is False

    def test_empty_string_is_default_false(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_ESCALATION_ENABLED", "")
        assert sbt_escalation_enabled() is False

    def test_whitespace_is_default_false(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_ESCALATION_ENABLED", "   ")
        assert sbt_escalation_enabled() is False

    @pytest.mark.parametrize(
        "truthy", ["1", "true", "yes", "on", "TRUE", "Yes"],
    )
    def test_truthy_values_enable(self, monkeypatch, truthy: str):
        monkeypatch.setenv("JARVIS_SBT_ESCALATION_ENABLED", truthy)
        assert sbt_escalation_enabled() is True

    @pytest.mark.parametrize(
        "falsy", ["0", "false", "no", "off", "False", "OFF"],
    )
    def test_falsy_values_disable(self, monkeypatch, falsy: str):
        monkeypatch.setenv("JARVIS_SBT_ESCALATION_ENABLED", falsy)
        assert sbt_escalation_enabled() is False


# ---------------------------------------------------------------------------
# Env-knob clamping
# ---------------------------------------------------------------------------


class TestEnvKnobClamping:
    def test_cost_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SBT_ESCALATION_MAX_COST_USD", raising=False,
        )
        assert max_escalation_cost_usd() == 0.10

    def test_cost_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SBT_ESCALATION_MAX_COST_USD", "0.0001",
        )
        assert max_escalation_cost_usd() == 0.01

    def test_cost_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SBT_ESCALATION_MAX_COST_USD", "999",
        )
        assert max_escalation_cost_usd() == 1.0

    def test_cost_garbage_uses_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SBT_ESCALATION_MAX_COST_USD", "not-a-float",
        )
        assert max_escalation_cost_usd() == 0.10

    def test_time_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SBT_ESCALATION_MAX_TIME_S", raising=False,
        )
        assert max_escalation_time_s() == 90.0

    def test_time_floor_and_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_ESCALATION_MAX_TIME_S", "0.5")
        assert max_escalation_time_s() == 10.0
        monkeypatch.setenv("JARVIS_SBT_ESCALATION_MAX_TIME_S", "9999")
        assert max_escalation_time_s() == 600.0


# ---------------------------------------------------------------------------
# Byte-parity to live enums (string-constant pin)
# ---------------------------------------------------------------------------


class TestByteParity:
    """The bridge module redefines ProbeOutcome / TreeVerdict /
    ConfidenceCollapseAction string values verbatim to stay
    pure-stdlib at hot path. These tests assert byte-parity to
    the live exports — if any upstream enum renames a value,
    the bridge fails BEFORE shipping out of sync."""

    def test_probe_outcome_byte_parity(self):
        from backend.core.ouroboros.governance.verification import (
            confidence_probe_bridge as live,
        )
        from backend.core.ouroboros.governance.verification.sbt_escalation_bridge import (
            _PROBE_OUTCOME_CONVERGED,
            _PROBE_OUTCOME_DIVERGED,
            _PROBE_OUTCOME_DISABLED,
            _PROBE_OUTCOME_EXHAUSTED,
            _PROBE_OUTCOME_FAILED,
        )
        assert _PROBE_OUTCOME_CONVERGED == live.ProbeOutcome.CONVERGED.value
        assert _PROBE_OUTCOME_DIVERGED == live.ProbeOutcome.DIVERGED.value
        assert _PROBE_OUTCOME_EXHAUSTED == live.ProbeOutcome.EXHAUSTED.value
        assert _PROBE_OUTCOME_DISABLED == live.ProbeOutcome.DISABLED.value
        assert _PROBE_OUTCOME_FAILED == live.ProbeOutcome.FAILED.value

    def test_tree_verdict_byte_parity(self):
        from backend.core.ouroboros.governance.verification import (
            speculative_branch as live,
        )
        from backend.core.ouroboros.governance.verification.sbt_escalation_bridge import (
            _TREE_VERDICT_CONVERGED,
            _TREE_VERDICT_DIVERGED,
            _TREE_VERDICT_FAILED,
            _TREE_VERDICT_INCONCLUSIVE,
            _TREE_VERDICT_TRUNCATED,
        )
        assert _TREE_VERDICT_CONVERGED == live.TreeVerdict.CONVERGED.value
        assert _TREE_VERDICT_DIVERGED == live.TreeVerdict.DIVERGED.value
        assert _TREE_VERDICT_INCONCLUSIVE == live.TreeVerdict.INCONCLUSIVE.value
        assert _TREE_VERDICT_TRUNCATED == live.TreeVerdict.TRUNCATED.value
        assert _TREE_VERDICT_FAILED == live.TreeVerdict.FAILED.value

    def test_collapse_action_byte_parity(self):
        from backend.core.ouroboros.governance.verification import (
            hypothesis_consumers as live,
        )
        from backend.core.ouroboros.governance.verification.sbt_escalation_bridge import (
            _COLLAPSE_ESCALATE_TO_OPERATOR,
            _COLLAPSE_INCONCLUSIVE,
            _COLLAPSE_RETRY_WITH_FEEDBACK,
        )
        assert _COLLAPSE_RETRY_WITH_FEEDBACK == (
            live.ConfidenceCollapseAction.RETRY_WITH_FEEDBACK.value
        )
        assert _COLLAPSE_ESCALATE_TO_OPERATOR == (
            live.ConfidenceCollapseAction.ESCALATE_TO_OPERATOR.value
        )
        assert _COLLAPSE_INCONCLUSIVE == (
            live.ConfidenceCollapseAction.INCONCLUSIVE.value
        )


# ---------------------------------------------------------------------------
# Authority invariant — pure-stdlib at hot path
# ---------------------------------------------------------------------------


class TestPureStdlibInvariant:
    def _source(self) -> str:
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "verification" / "sbt_escalation_bridge.py"
        )
        return path.read_text()

    def test_no_governance_imports_at_module_top(self):
        """Slice 1 stays pure-stdlib (registration-contract
        exemption applies — n/a here since Slice 3 adds the
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
                    if any(
                        s <= lineno <= e for s, e in exempt_ranges
                    ):
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
        assert SBT_ESCALATION_BRIDGE_SCHEMA_VERSION == "sbt_escalation_bridge.1"
