"""Slice 1 additive-field tests for the existing SkillManifest.

Verifies the SkillRegistry-AutonomousReach arc's two new fields
(``reach`` + ``trigger_specs``) without disturbing the existing
``test_skill_manifest.py`` 100+ test surface.

Coverage:
  * Default values preserve backward-compat
    (reach=OPERATOR_PLUS_MODEL, trigger_specs=())
  * from_mapping parses + validates both fields
  * Unknown reach raises SkillManifestError (re-raised from
    SkillTriggerError to preserve the existing dialect contract)
  * Malformed trigger_specs raises SkillManifestError
  * project() includes both fields in the JSON projection
  * SkillReach + SkillTriggerSpec re-exported via skill_manifest
    so callers don't need a separate import
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.skill_manifest import (
    SkillManifest,
    SkillManifestError,
    SkillReach,
    SkillTriggerError,
    SkillTriggerSpec,
)
from backend.core.ouroboros.governance.skill_trigger import (
    SkillTriggerKind,
)


def _minimal_mapping():
    return {
        "name": "x",
        "description": "d",
        "trigger": "t",
        "entrypoint": "mod.x:f",
    }


# ---------------------------------------------------------------------------
# Defaults preserve backward-compat
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_reach_default_is_operator_plus_model(self):
        m = SkillManifest.from_mapping(_minimal_mapping())
        assert m.reach is SkillReach.OPERATOR_PLUS_MODEL

    def test_trigger_specs_default_empty_tuple(self):
        m = SkillManifest.from_mapping(_minimal_mapping())
        assert m.trigger_specs == ()
        assert isinstance(m.trigger_specs, tuple)

    def test_omitting_new_fields_does_not_raise(self):
        # The existing minimal mapping (no reach, no trigger_specs)
        # must build a manifest exactly as before.
        m = SkillManifest.from_mapping(_minimal_mapping())
        assert m.name == "x"
        assert m.entrypoint == "mod.x:f"


# ---------------------------------------------------------------------------
# reach parsing
# ---------------------------------------------------------------------------


class TestReachParse:
    @pytest.mark.parametrize("raw, expected", [
        ("operator", SkillReach.OPERATOR),
        ("model", SkillReach.MODEL),
        ("autonomous", SkillReach.AUTONOMOUS),
        ("operator_plus_model", SkillReach.OPERATOR_PLUS_MODEL),
        ("any", SkillReach.ANY),
    ])
    def test_known_values_accepted(self, raw, expected):
        m = SkillManifest.from_mapping({
            **_minimal_mapping(), "reach": raw,
        })
        assert m.reach is expected

    def test_case_insensitive_normalization(self):
        m = SkillManifest.from_mapping({
            **_minimal_mapping(), "reach": "AUTONOMOUS",
        })
        assert m.reach is SkillReach.AUTONOMOUS

    def test_whitespace_tolerated(self):
        m = SkillManifest.from_mapping({
            **_minimal_mapping(), "reach": "  any  ",
        })
        assert m.reach is SkillReach.ANY

    def test_unknown_reach_raises_loud(self):
        with pytest.raises(SkillManifestError, match="unknown reach"):
            SkillManifest.from_mapping({
                **_minimal_mapping(), "reach": "god_mode",
            })

    def test_empty_reach_raises(self):
        with pytest.raises(SkillManifestError):
            SkillManifest.from_mapping({
                **_minimal_mapping(), "reach": "",
            })

    def test_non_string_reach_raises(self):
        with pytest.raises(SkillManifestError):
            SkillManifest.from_mapping({
                **_minimal_mapping(), "reach": 42,
            })

    def test_explicit_none_falls_back_to_default(self):
        # YAML produces None for `reach: ` -- treat as "use default"
        # rather than fail. Matches the existing _opt_str pattern.
        m = SkillManifest.from_mapping({
            **_minimal_mapping(), "reach": None,
        })
        assert m.reach is SkillReach.OPERATOR_PLUS_MODEL


# ---------------------------------------------------------------------------
# trigger_specs parsing
# ---------------------------------------------------------------------------


class TestTriggerSpecsParse:
    def test_single_spec(self):
        m = SkillManifest.from_mapping({
            **_minimal_mapping(),
            "trigger_specs": [{"kind": "sensor_fired"}],
        })
        assert len(m.trigger_specs) == 1
        assert isinstance(m.trigger_specs[0], SkillTriggerSpec)
        assert m.trigger_specs[0].kind is SkillTriggerKind.SENSOR_FIRED

    def test_multiple_specs(self):
        m = SkillManifest.from_mapping({
            **_minimal_mapping(),
            "trigger_specs": [
                {"kind": "sensor_fired"},
                {
                    "kind": "posture_transition",
                    "required_posture": "HARDEN",
                    "max_invocations": 2,
                },
            ],
        })
        assert len(m.trigger_specs) == 2
        assert (
            m.trigger_specs[1].required_posture == "HARDEN"
        )
        assert m.trigger_specs[1].max_invocations == 2

    def test_empty_list_yields_empty_tuple(self):
        m = SkillManifest.from_mapping({
            **_minimal_mapping(), "trigger_specs": [],
        })
        assert m.trigger_specs == ()

    def test_none_yields_empty_tuple(self):
        m = SkillManifest.from_mapping({
            **_minimal_mapping(), "trigger_specs": None,
        })
        assert m.trigger_specs == ()

    def test_unknown_kind_raises_loud(self):
        with pytest.raises(
            SkillManifestError, match="unknown trigger kind",
        ):
            SkillManifest.from_mapping({
                **_minimal_mapping(),
                "trigger_specs": [{"kind": "ghost_kind"}],
            })

    def test_unknown_field_in_spec_raises(self):
        with pytest.raises(SkillManifestError, match="unknown key"):
            SkillManifest.from_mapping({
                **_minimal_mapping(),
                "trigger_specs": [
                    {"kind": "sensor_fired", "secret_field": "x"},
                ],
            })

    def test_negative_max_invocations_raises(self):
        with pytest.raises(SkillManifestError, match="must be >= 0"):
            SkillManifest.from_mapping({
                **_minimal_mapping(),
                "trigger_specs": [
                    {"kind": "sensor_fired", "max_invocations": -1},
                ],
            })

    def test_negative_window_s_raises(self):
        with pytest.raises(SkillManifestError, match="must be >= 0"):
            SkillManifest.from_mapping({
                **_minimal_mapping(),
                "trigger_specs": [
                    {"kind": "sensor_fired", "window_s": -1.0},
                ],
            })

    def test_non_list_trigger_specs_raises(self):
        with pytest.raises(SkillManifestError, match="must be a list"):
            SkillManifest.from_mapping({
                **_minimal_mapping(),
                "trigger_specs": {"kind": "sensor_fired"},
            })

    def test_missing_kind_in_spec_raises(self):
        with pytest.raises(
            SkillManifestError, match="missing required field 'kind'",
        ):
            SkillManifest.from_mapping({
                **_minimal_mapping(),
                "trigger_specs": [{"signal_pattern": "x"}],
            })

    def test_error_includes_index_in_path(self):
        # Second element malformed -> error message shows index.
        with pytest.raises(SkillManifestError, match=r"trigger_specs\[1\]"):
            SkillManifest.from_mapping({
                **_minimal_mapping(),
                "trigger_specs": [
                    {"kind": "sensor_fired"},
                    {"kind": "ghost_kind"},
                ],
            })


# ---------------------------------------------------------------------------
# project() includes new fields
# ---------------------------------------------------------------------------


class TestProjectIncludesNewFields:
    def test_default_projection_has_reach_and_specs(self):
        proj = SkillManifest.from_mapping(_minimal_mapping()).project()
        assert proj["reach"] == "operator_plus_model"
        assert proj["trigger_specs"] == []

    def test_full_projection(self):
        proj = SkillManifest.from_mapping({
            **_minimal_mapping(),
            "reach": "any",
            "trigger_specs": [{
                "kind": "drift_detected",
                "required_drift_kind": "RECURRENCE_DRIFT",
                "max_invocations": 3,
                "window_s": 120.0,
            }],
        }).project()
        assert proj["reach"] == "any"
        assert len(proj["trigger_specs"]) == 1
        spec = proj["trigger_specs"][0]
        assert spec["kind"] == "drift_detected"
        assert spec["required_drift_kind"] == "RECURRENCE_DRIFT"
        assert spec["max_invocations"] == 3
        assert spec["window_s"] == 120.0


# ---------------------------------------------------------------------------
# Re-export ergonomics
# ---------------------------------------------------------------------------


class TestReExports:
    def test_skill_reach_reexported(self):
        # Callers can import SkillReach from skill_manifest -- no
        # separate import needed for the common case.
        from backend.core.ouroboros.governance.skill_manifest import (
            SkillReach as ReachFromManifest,
        )
        from backend.core.ouroboros.governance.skill_trigger import (
            SkillReach as ReachFromTrigger,
        )
        assert ReachFromManifest is ReachFromTrigger

    def test_skill_trigger_spec_reexported(self):
        from backend.core.ouroboros.governance.skill_manifest import (
            SkillTriggerSpec as SpecFromManifest,
        )
        from backend.core.ouroboros.governance.skill_trigger import (
            SkillTriggerSpec as SpecFromTrigger,
        )
        assert SpecFromManifest is SpecFromTrigger

    def test_skill_trigger_error_reexported(self):
        # SkillTriggerError is the strict-dialect failure raised by
        # the parsers -- skill_manifest re-exports it so callers
        # catching dialect errors see one symbol, even when the
        # error originates in skill_trigger.
        from backend.core.ouroboros.governance.skill_manifest import (
            SkillTriggerError as ErrFromManifest,
        )
        assert ErrFromManifest is SkillTriggerError


# ---------------------------------------------------------------------------
# Integration -- composing additive fields with existing surface
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_full_manifest_with_all_fields(self):
        m = SkillManifest.from_mapping({
            "name": "posture-correct",
            "description": "Snap posture back when drift detected.",
            "trigger": "Fire when Coherence Auditor flags drift.",
            "entrypoint": "ouroboros_skills.posture_correct:run",
            "version": "1.2.0",
            "author": "Ouroboros",
            "permissions": ["read_only"],
            "args_schema": {
                "dry_run": {"type": "boolean", "default": False},
            },
            "reach": "autonomous",
            "trigger_specs": [{
                "kind": "drift_detected",
                "signal_pattern": "coherence.drift_detected",
                "required_drift_kind": "RECURRENCE_DRIFT",
                "max_invocations": 1,
                "window_s": 600.0,
                "dedup_key_template": "{drift_kind}",
            }],
        })
        assert m.qualified_name == "posture-correct"
        assert m.reach is SkillReach.AUTONOMOUS
        assert len(m.trigger_specs) == 1
        assert m.trigger_specs[0].kind is (
            SkillTriggerKind.DRIFT_DETECTED
        )
        assert m.trigger_specs[0].dedup_key_template == "{drift_kind}"
        # Existing fields still populated.
        assert "read_only" in m.permissions
        assert m.args_schema["dry_run"]["default"] is False
        # Projection includes everything.
        proj = m.project()
        assert proj["reach"] == "autonomous"
        assert proj["trigger_specs"][0]["required_drift_kind"] == (
            "RECURRENCE_DRIFT"
        )
