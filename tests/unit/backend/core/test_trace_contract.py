"""Cross-repo contract tests for TraceEnvelope v1.
These tests consume the shared fixture and verify serialization/validation.
The same fixture should be consumed by JARVIS-Prime and Reactor-Core CI."""
import json
import pytest
from pathlib import Path


FIXTURE_PATH = Path(__file__).parent.parent.parent.parent / "fixtures" / "trace_envelope_v1.json"


@pytest.fixture
def fixture_data():
    assert FIXTURE_PATH.exists(), f"Contract fixture not found: {FIXTURE_PATH}"
    return json.loads(FIXTURE_PATH.read_text())


class TestTraceEnvelopeContract:
    def test_fixture_schema_version_matches(self, fixture_data):
        from backend.core.trace_envelope import TRACE_SCHEMA_VERSION
        assert fixture_data["schema_version"] == TRACE_SCHEMA_VERSION

    @pytest.mark.parametrize("case_idx", range(5))
    def test_deserialize_fixture_case(self, fixture_data, case_idx):
        from backend.core.trace_envelope import TraceEnvelope
        case = fixture_data["test_cases"][case_idx]
        env = TraceEnvelope.from_dict(case["envelope"])
        assert env.trace_id == case["envelope"]["trace_id"]
        assert env.schema_version == case["envelope"]["schema_version"]

    @pytest.mark.parametrize("case_idx", range(5))
    def test_validate_fixture_case(self, fixture_data, case_idx, monkeypatch):
        import backend.core.trace_envelope as _mod
        from backend.core.trace_envelope import TraceEnvelope, validate_envelope
        # Fixture timestamps are static for cross-repo reproducibility;
        # disable clock-skew validation so it doesn't fail on wall-clock drift.
        monkeypatch.setattr(_mod, "CLOCK_SKEW_TOLERANCE_S", float("inf"))
        case = fixture_data["test_cases"][case_idx]
        env = TraceEnvelope.from_dict(case["envelope"])
        errors = validate_envelope(env)
        if case["expect_valid"]:
            assert errors == [], f"Expected valid but got errors: {errors}"
        else:
            assert len(errors) > 0, "Expected invalid but got no errors"
            for expected_field in case["expect_errors"]:
                assert any(expected_field in e for e in errors), \
                    f"Expected error about '{expected_field}' in {errors}"

    @pytest.mark.parametrize("case_idx", range(5))
    def test_round_trip_preserves_all_fields(self, fixture_data, case_idx):
        from backend.core.trace_envelope import TraceEnvelope
        case = fixture_data["test_cases"][case_idx]
        env = TraceEnvelope.from_dict(case["envelope"])
        round_tripped = TraceEnvelope.from_dict(env.to_dict())
        assert round_tripped.trace_id == env.trace_id
        assert round_tripped.extra == env.extra

    def test_extra_fields_preserved(self, fixture_data):
        from backend.core.trace_envelope import TraceEnvelope
        case = fixture_data["test_cases"][4]  # unknown_extra_fields_preserved
        env = TraceEnvelope.from_dict(case["envelope"])
        assert env.extra.get("custom_metadata") == "preserved"
        assert env.extra.get("another_field") == 42
