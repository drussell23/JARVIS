"""SkillTrigger Slice 1 -- regression spine.

Pins the typed vocabulary + total decision function for the
SkillRegistry-AutonomousReach arc:

  * Closed-5 enums (SkillReach / SkillTriggerKind / SkillOutcome)
  * Frozen dataclasses (SkillTriggerSpec / SkillInvocation /
    SkillResult) -- mutation guards + to_dict round-trip
  * Master-flag asymmetric env semantics
  * Env-knob clamping (skill_per_window_max_invocations /
    skill_window_default_s) -- floor + ceiling honored
  * reach_includes lattice (ANY covers all; OPERATOR_PLUS_MODEL
    covers operator + model only; everything else covers self)
  * compute_should_fire total decision tree -- every input
    combination maps to exactly one outcome; NEVER raises
  * Strict dialect validators: parse_reach / parse_trigger_kind
    / parse_trigger_spec_mapping / parse_trigger_specs_list
    fail loudly on malformed input
  * compute_dedup_key template substitution + structural fallback
  * AST-walked authority invariants (pure-stdlib / no async /
    no exec/eval/compile)
"""
from __future__ import annotations

import ast
import enum
import pathlib
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from backend.core.ouroboros.governance.skill_trigger import (
    SKILL_TRIGGER_SCHEMA_VERSION,
    SkillInvocation,
    SkillOutcome,
    SkillReach,
    SkillResult,
    SkillTriggerError,
    SkillTriggerKind,
    SkillTriggerSpec,
    VALID_OUTCOMES,
    VALID_REACHES,
    VALID_TRIGGER_KINDS,
    compute_dedup_key,
    compute_should_fire,
    parse_reach,
    parse_trigger_kind,
    parse_trigger_spec_mapping,
    parse_trigger_specs_list,
    reach_includes,
    skill_per_window_max_invocations,
    skill_trigger_enabled,
    skill_window_default_s,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_SKILL_TRIGGER_ENABLED",
        "JARVIS_SKILL_PER_WINDOW_MAX",
        "JARVIS_SKILL_WINDOW_S",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Closed-taxonomy invariants
# ---------------------------------------------------------------------------


class TestClosedTaxonomies:
    def test_reach_has_exactly_five_values(self):
        assert len(list(SkillReach)) == 5

    def test_reach_value_set_exact(self):
        expected = {
            "operator", "model", "autonomous",
            "operator_plus_model", "any",
        }
        actual = {v.value for v in SkillReach}
        assert actual == expected
        assert VALID_REACHES == expected

    def test_trigger_kind_has_exactly_five_values(self):
        assert len(list(SkillTriggerKind)) == 5

    def test_trigger_kind_value_set_exact(self):
        expected = {
            "posture_transition", "drift_detected", "sensor_fired",
            "explicit_invocation", "disabled",
        }
        actual = {v.value for v in SkillTriggerKind}
        assert actual == expected
        assert VALID_TRIGGER_KINDS == expected

    def test_outcome_has_exactly_five_values(self):
        assert len(list(SkillOutcome)) == 5

    def test_outcome_value_set_exact(self):
        expected = {
            "invoked", "skipped_precondition", "skipped_disabled",
            "denied_policy", "failed",
        }
        actual = {v.value for v in SkillOutcome}
        assert actual == expected
        assert VALID_OUTCOMES == expected


# ---------------------------------------------------------------------------
# Frozen dataclass mutation guards + to_dict round-trip
# ---------------------------------------------------------------------------


class TestFrozenGuards:
    def test_trigger_spec_frozen(self):
        s = SkillTriggerSpec(kind=SkillTriggerKind.SENSOR_FIRED)
        with pytest.raises(FrozenInstanceError):
            s.kind = SkillTriggerKind.DRIFT_DETECTED  # type: ignore[misc]

    def test_invocation_frozen(self):
        inv = SkillInvocation(
            skill_name="x",
            triggered_by_kind=SkillTriggerKind.SENSOR_FIRED,
        )
        with pytest.raises(FrozenInstanceError):
            inv.skill_name = "y"  # type: ignore[misc]

    def test_result_frozen(self):
        r = SkillResult(
            outcome=SkillOutcome.INVOKED, skill_name="x",
        )
        with pytest.raises(FrozenInstanceError):
            r.outcome = SkillOutcome.FAILED  # type: ignore[misc]


class TestToDictRoundTrip:
    def test_trigger_spec_to_dict(self):
        s = SkillTriggerSpec(
            kind=SkillTriggerKind.POSTURE_TRANSITION,
            signal_pattern="posture.changed",
            required_posture="HARDEN",
            max_invocations=10,
            window_s=300.0,
            dedup_key_template="{posture}",
        )
        d = s.to_dict()
        assert d["kind"] == "posture_transition"
        assert d["required_posture"] == "HARDEN"
        assert d["max_invocations"] == 10
        assert d["window_s"] == 300.0
        assert d["dedup_key_template"] == "{posture}"
        assert d["schema_version"] == SKILL_TRIGGER_SCHEMA_VERSION

    def test_invocation_to_dict(self):
        inv = SkillInvocation(
            skill_name="x",
            triggered_by_kind=SkillTriggerKind.DRIFT_DETECTED,
            triggered_by_signal="coherence.drift_detected",
            triggered_at_monotonic=42.0,
            arguments={"a": 1},
            payload={"drift_kind": "RECURRENCE_DRIFT"},
            caller_op_id="op-1",
        )
        d = inv.to_dict()
        assert d["skill_name"] == "x"
        assert d["triggered_by_kind"] == "drift_detected"
        assert d["payload"]["drift_kind"] == "RECURRENCE_DRIFT"
        assert d["caller_op_id"] == "op-1"

    def test_result_to_dict(self):
        r = SkillResult(
            outcome=SkillOutcome.INVOKED,
            skill_name="x",
            reason="ok",
            matched_trigger_index=2,
            monotonic_tightening_verdict="passed",
        )
        d = r.to_dict()
        assert d["outcome"] == "invoked"
        assert d["matched_trigger_index"] == 2
        assert d["monotonic_tightening_verdict"] == "passed"


class TestResultProperties:
    def test_is_invoked(self):
        assert SkillResult(
            outcome=SkillOutcome.INVOKED, skill_name="x",
        ).is_invoked is True
        for o in SkillOutcome:
            if o is SkillOutcome.INVOKED:
                continue
            assert SkillResult(
                outcome=o, skill_name="x",
            ).is_invoked is False

    def test_is_tightening_only_invoked(self):
        for o in SkillOutcome:
            r = SkillResult(outcome=o, skill_name="x")
            assert r.is_tightening is (o is SkillOutcome.INVOKED)


# ---------------------------------------------------------------------------
# Master flag asymmetric env semantics
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_false_pre_graduation(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SKILL_TRIGGER_ENABLED", raising=False)
        assert skill_trigger_enabled() is False

    def test_empty_string_is_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SKILL_TRIGGER_ENABLED", "")
        assert skill_trigger_enabled() is False

    def test_whitespace_is_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SKILL_TRIGGER_ENABLED", "   ")
        assert skill_trigger_enabled() is False

    @pytest.mark.parametrize(
        "raw", ["1", "true", "TRUE", "yes", "On", "ON"],
    )
    def test_truthy_enables(self, monkeypatch, raw):
        monkeypatch.setenv("JARVIS_SKILL_TRIGGER_ENABLED", raw)
        assert skill_trigger_enabled() is True

    @pytest.mark.parametrize(
        "raw", ["0", "false", "FALSE", "no", "off", "garbage"],
    )
    def test_falsy_disables(self, monkeypatch, raw):
        monkeypatch.setenv("JARVIS_SKILL_TRIGGER_ENABLED", raw)
        assert skill_trigger_enabled() is False


# ---------------------------------------------------------------------------
# Env-knob clamping (floor + ceiling)
# ---------------------------------------------------------------------------


class TestEnvKnobClamping:
    def test_per_window_max_default(self, monkeypatch):
        assert skill_per_window_max_invocations() == 5

    def test_per_window_max_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SKILL_PER_WINDOW_MAX", "0")
        assert skill_per_window_max_invocations() == 1

    def test_per_window_max_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SKILL_PER_WINDOW_MAX", "9999")
        assert skill_per_window_max_invocations() == 100

    def test_per_window_max_garbage(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SKILL_PER_WINDOW_MAX", "abc")
        assert skill_per_window_max_invocations() == 5

    def test_window_default_s(self, monkeypatch):
        assert skill_window_default_s() == 60.0

    def test_window_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SKILL_WINDOW_S", "0.0")
        assert skill_window_default_s() == 1.0

    def test_window_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SKILL_WINDOW_S", "100000")
        assert skill_window_default_s() == 3600.0


# ---------------------------------------------------------------------------
# reach_includes lattice
# ---------------------------------------------------------------------------


class TestReachIncludes:
    def test_any_covers_all(self):
        for target in SkillReach:
            assert reach_includes(SkillReach.ANY, target) is True

    def test_operator_plus_model_covers_op_and_model(self):
        assert reach_includes(
            SkillReach.OPERATOR_PLUS_MODEL, SkillReach.OPERATOR,
        ) is True
        assert reach_includes(
            SkillReach.OPERATOR_PLUS_MODEL, SkillReach.MODEL,
        ) is True

    def test_operator_plus_model_excludes_autonomous(self):
        assert reach_includes(
            SkillReach.OPERATOR_PLUS_MODEL, SkillReach.AUTONOMOUS,
        ) is False

    def test_singleton_reaches_only_match_self(self):
        for r in (SkillReach.OPERATOR, SkillReach.MODEL,
                  SkillReach.AUTONOMOUS):
            for t in SkillReach:
                if t is r:
                    assert reach_includes(r, t) is True
                elif t is SkillReach.ANY:
                    # ANY as TARGET is not implied; only as REACH.
                    assert reach_includes(r, t) is False
                else:
                    assert reach_includes(r, t) is False

    def test_garbage_returns_false(self):
        assert reach_includes("operator", SkillReach.OPERATOR) is False  # type: ignore[arg-type]
        assert reach_includes(SkillReach.ANY, "model") is False  # type: ignore[arg-type]
        assert reach_includes(None, None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Strict dialect validators
# ---------------------------------------------------------------------------


class TestParseReach:
    def test_passthrough_enum(self):
        assert parse_reach(SkillReach.AUTONOMOUS) is SkillReach.AUTONOMOUS

    def test_string_normalizes(self):
        assert parse_reach("Operator") is SkillReach.OPERATOR
        assert parse_reach("  ANY  ") is SkillReach.ANY

    def test_unknown_raises_loud(self):
        with pytest.raises(SkillTriggerError, match="unknown reach"):
            parse_reach("god_mode")

    def test_empty_raises(self):
        with pytest.raises(SkillTriggerError):
            parse_reach("")
        with pytest.raises(SkillTriggerError):
            parse_reach("   ")

    def test_non_string_raises(self):
        with pytest.raises(SkillTriggerError):
            parse_reach(42)
        with pytest.raises(SkillTriggerError):
            parse_reach(None)


class TestParseTriggerKind:
    def test_passthrough_enum(self):
        assert parse_trigger_kind(
            SkillTriggerKind.SENSOR_FIRED,
        ) is SkillTriggerKind.SENSOR_FIRED

    def test_string_normalizes(self):
        assert parse_trigger_kind(
            "POSTURE_TRANSITION",
        ) is SkillTriggerKind.POSTURE_TRANSITION

    def test_unknown_raises(self):
        with pytest.raises(SkillTriggerError, match="unknown trigger kind"):
            parse_trigger_kind("clairvoyance")


class TestParseTriggerSpecMapping:
    def test_minimal(self):
        spec = parse_trigger_spec_mapping({"kind": "sensor_fired"})
        assert spec.kind is SkillTriggerKind.SENSOR_FIRED
        assert spec.required_posture == ""
        assert spec.max_invocations == 0
        assert spec.window_s == 0.0

    def test_full(self):
        spec = parse_trigger_spec_mapping({
            "kind": "posture_transition",
            "signal_pattern": "posture.changed",
            "required_posture": "HARDEN",
            "max_invocations": 3,
            "window_s": 120.0,
            "dedup_key_template": "{posture}",
        })
        assert spec.kind is SkillTriggerKind.POSTURE_TRANSITION
        assert spec.required_posture == "HARDEN"
        assert spec.max_invocations == 3
        assert spec.window_s == 120.0
        assert spec.dedup_key_template == "{posture}"

    def test_missing_kind_raises(self):
        with pytest.raises(
            SkillTriggerError, match="missing required field 'kind'",
        ):
            parse_trigger_spec_mapping({"signal_pattern": "x"})

    def test_unknown_key_raises(self):
        with pytest.raises(SkillTriggerError, match="unknown key"):
            parse_trigger_spec_mapping({
                "kind": "sensor_fired", "secret_field": "x",
            })

    def test_non_mapping_raises(self):
        with pytest.raises(SkillTriggerError, match="must be a mapping"):
            parse_trigger_spec_mapping([])  # type: ignore[arg-type]
        with pytest.raises(SkillTriggerError):
            parse_trigger_spec_mapping("oops")  # type: ignore[arg-type]

    def test_negative_max_invocations_raises(self):
        with pytest.raises(SkillTriggerError, match="must be >= 0"):
            parse_trigger_spec_mapping({
                "kind": "sensor_fired", "max_invocations": -1,
            })

    def test_negative_window_raises(self):
        with pytest.raises(SkillTriggerError, match="must be >= 0"):
            parse_trigger_spec_mapping({
                "kind": "sensor_fired", "window_s": -1.0,
            })

    def test_bool_max_invocations_raises(self):
        with pytest.raises(SkillTriggerError):
            parse_trigger_spec_mapping({
                "kind": "sensor_fired", "max_invocations": True,
            })

    def test_bool_window_raises(self):
        with pytest.raises(SkillTriggerError):
            parse_trigger_spec_mapping({
                "kind": "sensor_fired", "window_s": True,
            })

    def test_string_max_invocations_raises(self):
        with pytest.raises(SkillTriggerError, match="must be an integer"):
            parse_trigger_spec_mapping({
                "kind": "sensor_fired", "max_invocations": "10",
            })

    def test_non_string_signal_pattern_raises(self):
        with pytest.raises(SkillTriggerError):
            parse_trigger_spec_mapping({
                "kind": "sensor_fired", "signal_pattern": 42,
            })


class TestParseTriggerSpecsList:
    def test_none_returns_empty(self):
        assert parse_trigger_specs_list(None) == ()

    def test_empty_list(self):
        assert parse_trigger_specs_list([]) == ()

    def test_multiple(self):
        out = parse_trigger_specs_list([
            {"kind": "sensor_fired"},
            {"kind": "posture_transition", "required_posture": "HARDEN"},
        ])
        assert len(out) == 2
        assert out[0].kind is SkillTriggerKind.SENSOR_FIRED
        assert out[1].required_posture == "HARDEN"

    def test_non_list_raises(self):
        with pytest.raises(SkillTriggerError, match="must be a list"):
            parse_trigger_specs_list({"kind": "sensor_fired"})

    def test_malformed_element_raises_with_index(self):
        with pytest.raises(
            SkillTriggerError, match=r"trigger_specs\[1\]",
        ):
            parse_trigger_specs_list([
                {"kind": "sensor_fired"},
                {"kind": "ghost_kind"},
            ])


# ---------------------------------------------------------------------------
# compute_should_fire decision tree
# ---------------------------------------------------------------------------


def _make_manifest(
    *,
    name: str = "x",
    reach: SkillReach = SkillReach.ANY,
    trigger_specs: Any = (),
    risk_class: str = "safe_auto",
):
    """Lightweight manifest stub for testing -- duck-typed via
    getattr inside compute_should_fire."""
    class _M:
        pass
    m = _M()
    m.name = name
    m.reach = reach
    m.trigger_specs = trigger_specs
    m.risk_class = risk_class
    return m


def _autonomous_invocation(
    skill_name: str = "x",
    *,
    kind: SkillTriggerKind = SkillTriggerKind.SENSOR_FIRED,
    payload: dict = None,
) -> SkillInvocation:
    return SkillInvocation(
        skill_name=skill_name,
        triggered_by_kind=kind,
        triggered_by_signal="x",
        payload=payload or {},
    )


class TestComputeShouldFire_MasterGate:
    def test_disabled_explicit(self):
        m = _make_manifest()
        out = compute_should_fire(
            m, _autonomous_invocation(), enabled=False,
        )
        assert out.outcome is SkillOutcome.SKIPPED_DISABLED
        assert out.skill_name == "x"

    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SKILL_TRIGGER_ENABLED", raising=False)
        m = _make_manifest()
        out = compute_should_fire(m, _autonomous_invocation())
        assert out.outcome is SkillOutcome.SKIPPED_DISABLED


class TestComputeShouldFire_DefensiveGuards:
    def test_none_manifest_failed(self):
        out = compute_should_fire(
            None, _autonomous_invocation(), enabled=True,
        )
        assert out.outcome is SkillOutcome.FAILED

    def test_invocation_wrong_type_failed(self):
        m = _make_manifest()
        out = compute_should_fire(
            m, "not an invocation", enabled=True,  # type: ignore[arg-type]
        )
        assert out.outcome is SkillOutcome.FAILED

    def test_manifest_missing_name_failed(self):
        class _M: pass
        m = _M()
        m.reach = SkillReach.ANY
        m.trigger_specs = ()
        m.risk_class = "safe_auto"
        out = compute_should_fire(
            m, _autonomous_invocation(), enabled=True,
        )
        assert out.outcome is SkillOutcome.FAILED

    def test_manifest_reach_wrong_type_failed(self):
        class _M: pass
        m = _M()
        m.name = "x"
        m.reach = "operator"  # string instead of enum
        m.trigger_specs = ()
        m.risk_class = "safe_auto"
        out = compute_should_fire(
            m, _autonomous_invocation(), enabled=True,
        )
        assert out.outcome is SkillOutcome.FAILED


class TestComputeShouldFire_ReachGate:
    def test_model_only_excludes_autonomous(self):
        m = _make_manifest(reach=SkillReach.MODEL)
        out = compute_should_fire(
            m, _autonomous_invocation(), enabled=True,
        )
        assert out.outcome is SkillOutcome.SKIPPED_DISABLED
        assert "model" in out.reason

    def test_autonomous_only_excludes_explicit(self):
        m = _make_manifest(reach=SkillReach.AUTONOMOUS)
        inv = SkillInvocation(
            skill_name="x",
            triggered_by_kind=SkillTriggerKind.EXPLICIT_INVOCATION,
        )
        out = compute_should_fire(m, inv, enabled=True)
        assert out.outcome is SkillOutcome.SKIPPED_DISABLED

    def test_operator_plus_model_accepts_explicit(self):
        m = _make_manifest(reach=SkillReach.OPERATOR_PLUS_MODEL)
        inv = SkillInvocation(
            skill_name="x",
            triggered_by_kind=SkillTriggerKind.EXPLICIT_INVOCATION,
        )
        out = compute_should_fire(m, inv, enabled=True)
        assert out.outcome is SkillOutcome.INVOKED

    def test_operator_plus_model_rejects_autonomous(self):
        m = _make_manifest(reach=SkillReach.OPERATOR_PLUS_MODEL)
        out = compute_should_fire(
            m, _autonomous_invocation(), enabled=True,
        )
        assert out.outcome is SkillOutcome.SKIPPED_DISABLED

    def test_any_accepts_everything(self):
        m = _make_manifest(
            reach=SkillReach.ANY,
            trigger_specs=(SkillTriggerSpec(
                kind=SkillTriggerKind.SENSOR_FIRED,
            ),),
        )
        # autonomous
        out = compute_should_fire(
            m, _autonomous_invocation(), enabled=True,
        )
        assert out.outcome is SkillOutcome.INVOKED
        # explicit
        out = compute_should_fire(
            m, SkillInvocation(
                skill_name="x",
                triggered_by_kind=SkillTriggerKind.EXPLICIT_INVOCATION,
            ),
            enabled=True,
        )
        assert out.outcome is SkillOutcome.INVOKED


class TestComputeShouldFire_RiskGate:
    def test_risk_floor_blocked_denies(self):
        m = _make_manifest()
        out = compute_should_fire(
            m, _autonomous_invocation(),
            risk_floor="blocked", enabled=True,
        )
        assert out.outcome is SkillOutcome.DENIED_POLICY

    def test_skill_risk_blocked_denies(self):
        m = _make_manifest(risk_class="blocked")
        out = compute_should_fire(
            m, _autonomous_invocation(), enabled=True,
        )
        assert out.outcome is SkillOutcome.DENIED_POLICY

    def test_safe_auto_passes(self):
        m = _make_manifest(
            risk_class="safe_auto",
            trigger_specs=(SkillTriggerSpec(
                kind=SkillTriggerKind.SENSOR_FIRED,
            ),),
        )
        out = compute_should_fire(
            m, _autonomous_invocation(),
            risk_floor="safe_auto", enabled=True,
        )
        assert out.outcome is SkillOutcome.INVOKED


class TestComputeShouldFire_TriggerMatching:
    def test_no_specs_autonomous_skipped(self):
        m = _make_manifest(trigger_specs=())
        out = compute_should_fire(
            m, _autonomous_invocation(), enabled=True,
        )
        assert out.outcome is SkillOutcome.SKIPPED_PRECONDITION

    def test_kind_mismatch_skipped(self):
        m = _make_manifest(trigger_specs=(
            SkillTriggerSpec(kind=SkillTriggerKind.DRIFT_DETECTED),
        ))
        # invocation is SENSOR_FIRED, spec wants DRIFT_DETECTED
        out = compute_should_fire(
            m, _autonomous_invocation(), enabled=True,
        )
        assert out.outcome is SkillOutcome.SKIPPED_PRECONDITION

    def test_kind_match_invokes(self):
        m = _make_manifest(trigger_specs=(
            SkillTriggerSpec(kind=SkillTriggerKind.SENSOR_FIRED),
        ))
        out = compute_should_fire(
            m, _autonomous_invocation(), enabled=True,
        )
        assert out.outcome is SkillOutcome.INVOKED
        assert out.matched_trigger_index == 0
        assert out.monotonic_tightening_verdict == "passed"

    def test_posture_mismatch_skipped(self):
        m = _make_manifest(trigger_specs=(
            SkillTriggerSpec(
                kind=SkillTriggerKind.POSTURE_TRANSITION,
                required_posture="HARDEN",
            ),
        ))
        inv = SkillInvocation(
            skill_name="x",
            triggered_by_kind=SkillTriggerKind.POSTURE_TRANSITION,
            payload={"posture": "EXPLORE"},
        )
        out = compute_should_fire(m, inv, enabled=True)
        assert out.outcome is SkillOutcome.SKIPPED_PRECONDITION

    def test_posture_match_invokes(self):
        m = _make_manifest(trigger_specs=(
            SkillTriggerSpec(
                kind=SkillTriggerKind.POSTURE_TRANSITION,
                required_posture="HARDEN",
            ),
        ))
        inv = SkillInvocation(
            skill_name="x",
            triggered_by_kind=SkillTriggerKind.POSTURE_TRANSITION,
            payload={"posture": "HARDEN"},
        )
        out = compute_should_fire(m, inv, enabled=True)
        assert out.outcome is SkillOutcome.INVOKED

    def test_posture_empty_required_matches_any(self):
        m = _make_manifest(trigger_specs=(
            SkillTriggerSpec(
                kind=SkillTriggerKind.POSTURE_TRANSITION,
                required_posture="",
            ),
        ))
        inv = SkillInvocation(
            skill_name="x",
            triggered_by_kind=SkillTriggerKind.POSTURE_TRANSITION,
            payload={"posture": "anything"},
        )
        out = compute_should_fire(m, inv, enabled=True)
        assert out.outcome is SkillOutcome.INVOKED

    def test_drift_match(self):
        m = _make_manifest(trigger_specs=(
            SkillTriggerSpec(
                kind=SkillTriggerKind.DRIFT_DETECTED,
                required_drift_kind="RECURRENCE_DRIFT",
            ),
        ))
        inv = SkillInvocation(
            skill_name="x",
            triggered_by_kind=SkillTriggerKind.DRIFT_DETECTED,
            payload={"drift_kind": "RECURRENCE_DRIFT"},
        )
        out = compute_should_fire(m, inv, enabled=True)
        assert out.outcome is SkillOutcome.INVOKED

    def test_sensor_match(self):
        m = _make_manifest(trigger_specs=(
            SkillTriggerSpec(
                kind=SkillTriggerKind.SENSOR_FIRED,
                required_sensor_name="test_failure",
            ),
        ))
        inv = SkillInvocation(
            skill_name="x",
            triggered_by_kind=SkillTriggerKind.SENSOR_FIRED,
            payload={"sensor_name": "test_failure"},
        )
        out = compute_should_fire(m, inv, enabled=True)
        assert out.outcome is SkillOutcome.INVOKED

    def test_first_matching_spec_wins(self):
        m = _make_manifest(trigger_specs=(
            SkillTriggerSpec(kind=SkillTriggerKind.DRIFT_DETECTED),
            SkillTriggerSpec(kind=SkillTriggerKind.SENSOR_FIRED),
            SkillTriggerSpec(kind=SkillTriggerKind.SENSOR_FIRED),  # dup
        ))
        out = compute_should_fire(
            m, _autonomous_invocation(), enabled=True,
        )
        assert out.outcome is SkillOutcome.INVOKED
        assert out.matched_trigger_index == 1  # first SENSOR_FIRED

    def test_disabled_kind_spec_never_matches(self):
        m = _make_manifest(trigger_specs=(
            SkillTriggerSpec(kind=SkillTriggerKind.DISABLED),
        ))
        out = compute_should_fire(
            m, _autonomous_invocation(), enabled=True,
        )
        assert out.outcome is SkillOutcome.SKIPPED_PRECONDITION

    def test_explicit_invocation_with_no_specs_invokes(self):
        m = _make_manifest(trigger_specs=())
        inv = SkillInvocation(
            skill_name="x",
            triggered_by_kind=SkillTriggerKind.EXPLICIT_INVOCATION,
        )
        out = compute_should_fire(m, inv, enabled=True)
        assert out.outcome is SkillOutcome.INVOKED
        assert out.matched_trigger_index is None

    def test_explicit_invocation_can_match_spec_too(self):
        m = _make_manifest(trigger_specs=(
            SkillTriggerSpec(
                kind=SkillTriggerKind.EXPLICIT_INVOCATION,
            ),
        ))
        inv = SkillInvocation(
            skill_name="x",
            triggered_by_kind=SkillTriggerKind.EXPLICIT_INVOCATION,
        )
        out = compute_should_fire(m, inv, enabled=True)
        assert out.outcome is SkillOutcome.INVOKED
        assert out.matched_trigger_index == 0


class TestComputeShouldFire_TighteningStamp:
    def test_invoked_stamps_passed(self):
        m = _make_manifest(trigger_specs=(
            SkillTriggerSpec(kind=SkillTriggerKind.SENSOR_FIRED),
        ))
        out = compute_should_fire(
            m, _autonomous_invocation(), enabled=True,
        )
        assert out.monotonic_tightening_verdict == "passed"

    @pytest.mark.parametrize(
        "outcome", [
            SkillOutcome.SKIPPED_DISABLED,
            SkillOutcome.SKIPPED_PRECONDITION,
            SkillOutcome.DENIED_POLICY,
            SkillOutcome.FAILED,
        ],
    )
    def test_non_invoked_empty_stamp(self, outcome):
        # Easiest way: drive each outcome via the matching gate.
        m = _make_manifest()
        if outcome is SkillOutcome.SKIPPED_DISABLED:
            r = compute_should_fire(
                m, _autonomous_invocation(), enabled=False,
            )
        elif outcome is SkillOutcome.DENIED_POLICY:
            r = compute_should_fire(
                m, _autonomous_invocation(),
                risk_floor="blocked", enabled=True,
            )
        elif outcome is SkillOutcome.FAILED:
            r = compute_should_fire(
                None, _autonomous_invocation(), enabled=True,
            )
        else:
            r = compute_should_fire(
                m, _autonomous_invocation(), enabled=True,
            )
        assert r.outcome is outcome
        assert r.monotonic_tightening_verdict == ""


# ---------------------------------------------------------------------------
# compute_dedup_key
# ---------------------------------------------------------------------------


class TestComputeDedupKey:
    def test_template_substitutes(self):
        spec = SkillTriggerSpec(
            kind=SkillTriggerKind.POSTURE_TRANSITION,
            dedup_key_template="posture:{posture}|skill:{skill_name}",
        )
        inv = SkillInvocation(
            skill_name="x",
            triggered_by_kind=SkillTriggerKind.POSTURE_TRANSITION,
            payload={"posture": "HARDEN"},
        )
        assert compute_dedup_key(inv, spec) == "posture:HARDEN|skill:x"

    def test_template_substitutes_multiple_fields(self):
        spec = SkillTriggerSpec(
            kind=SkillTriggerKind.DRIFT_DETECTED,
            dedup_key_template=(
                "{kind}|{drift_kind}|{sensor_name}|{signal}"
            ),
        )
        inv = SkillInvocation(
            skill_name="x",
            triggered_by_kind=SkillTriggerKind.DRIFT_DETECTED,
            triggered_by_signal="coherence.drift",
            payload={
                "drift_kind": "RECURRENCE",
                "sensor_name": "coherence",
            },
        )
        out = compute_dedup_key(inv, spec)
        assert out == "drift_detected|RECURRENCE|coherence|coherence.drift"

    def test_no_spec_uses_structural_fingerprint(self):
        inv = SkillInvocation(
            skill_name="x",
            triggered_by_kind=SkillTriggerKind.SENSOR_FIRED,
            triggered_by_signal="sensor.fired.test",
            payload={"sensor_name": "test_failure", "extra": "data"},
        )
        out = compute_dedup_key(inv, None)
        assert "x" in out
        assert "sensor_fired" in out
        assert "sensor.fired.test" in out
        assert "sensor_name=test_failure" in out
        # Sorted -> deterministic
        assert out == compute_dedup_key(inv, None)

    def test_garbage_invocation_returns_empty(self):
        assert compute_dedup_key("not an invocation") == ""  # type: ignore[arg-type]
        assert compute_dedup_key(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AST-walked authority invariants (Slice 5 will pin formally)
# ---------------------------------------------------------------------------


_TRIGGER_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "skill_trigger.py"
)


class TestAuthorityInvariants:
    @staticmethod
    def _source() -> str:
        return _TRIGGER_PATH.read_text()

    def test_pure_stdlib_no_governance_imports(self):
        source = self._source()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "backend." in module or "governance" in module:
                    raise AssertionError(
                        f"Slice 1 must be pure-stdlib -- found "
                        f"governance import {module!r} at line "
                        f"{getattr(node, 'lineno', '?')}"
                    )

    def test_no_async_def_in_module(self):
        source = self._source()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                raise AssertionError(
                    f"Slice 1 must be sync -- found async def "
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
                            f"Slice 1 must NOT exec/eval/compile -- "
                            f"found {node.func.id}() at line "
                            f"{getattr(node, 'lineno', '?')}"
                        )


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_constant(self):
        assert SKILL_TRIGGER_SCHEMA_VERSION == "skill_trigger.v1"

    def test_dataclass_default_matches_constant(self):
        assert SkillTriggerSpec(
            kind=SkillTriggerKind.SENSOR_FIRED,
        ).schema_version == SKILL_TRIGGER_SCHEMA_VERSION
        assert SkillInvocation(
            skill_name="x",
            triggered_by_kind=SkillTriggerKind.SENSOR_FIRED,
        ).schema_version == SKILL_TRIGGER_SCHEMA_VERSION
        assert SkillResult(
            outcome=SkillOutcome.INVOKED, skill_name="x",
        ).schema_version == SKILL_TRIGGER_SCHEMA_VERSION
