"""Regression spine for ``SignalSource.VISION_SENSOR`` + VisionSignalEvidence
schema v1.

Pins the Task 3 invariants from the implementation plan
(``docs/superpowers/plans/2026-04-18-vision-sensor-verify.md``) and §Sensor
Contract / §Invariant I1 in the design spec.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.intent.signals import (
    _VISION_APP_ID_MAX_LEN,
    _VISION_CLASSIFIER_MODEL_MAX_LEN,
    _VISION_DETERMINISTIC_MATCHES_MAX,
    _VISION_OCR_SNIPPET_MAX_LEN,
    _VISION_SIGNAL_SCHEMA_VERSION,
    _VISION_VALID_SEVERITIES,
    _VISION_VALID_VERDICTS,
    IntentSignal,
    SignalSource,
    VisionSignalEvidence,
    build_vision_signal_evidence,
    validate_vision_signal_evidence,
)


# ---------------------------------------------------------------------------
# Minimum-valid payload helper
# ---------------------------------------------------------------------------


_VALID_FRAME_HASH = "0123456789abcdef"  # 16 lowercase hex


def _minimum_valid_evidence(**overrides) -> VisionSignalEvidence:
    """Build a minimum-valid evidence payload; override any field via kwargs."""
    base = dict(
        frame_hash=_VALID_FRAME_HASH,
        frame_ts=1.0,
        frame_path="/tmp/frame.jpg",
        classifier_verdict="error_visible",
        classifier_model="deterministic",
        classifier_confidence=1.0,
        deterministic_matches=("traceback",),
        ocr_snippet="TypeError",
        severity="error",
    )
    base.update(overrides)
    return build_vision_signal_evidence(**base)


# ---------------------------------------------------------------------------
# SignalSource enum
# ---------------------------------------------------------------------------


def test_signal_source_vision_sensor_variant_present():
    assert SignalSource.VISION_SENSOR is not None


def test_signal_source_vision_sensor_value_is_vision_sensor():
    assert SignalSource.VISION_SENSOR.value == "vision_sensor"


def test_signal_source_is_str_enum_interop():
    # StrEnum-style interop: enum member compares equal to its string value
    assert SignalSource.VISION_SENSOR == "vision_sensor"
    # And coerces cleanly to str()
    assert str(SignalSource.VISION_SENSOR.value) == "vision_sensor"


def test_signal_source_hashable():
    # Members usable as dict keys / set members
    d = {SignalSource.VISION_SENSOR: "ok"}
    assert d[SignalSource.VISION_SENSOR] == "ok"


def test_signal_source_members_iterable():
    members = list(SignalSource)
    assert SignalSource.VISION_SENSOR in members


# ---------------------------------------------------------------------------
# build_vision_signal_evidence — happy paths
# ---------------------------------------------------------------------------


def test_build_returns_all_required_fields():
    e = _minimum_valid_evidence()
    required = {
        "schema_version",
        "frame_hash",
        "frame_ts",
        "frame_path",
        "app_id",
        "window_id",
        "classifier_verdict",
        "classifier_model",
        "classifier_confidence",
        "deterministic_matches",
        "ocr_snippet",
        "severity",
    }
    assert set(e.keys()) == required


def test_build_stamps_schema_version_1():
    e = _minimum_valid_evidence()
    assert e["schema_version"] == 1
    assert _VISION_SIGNAL_SCHEMA_VERSION == 1  # spec constant locked


def test_build_defaults_optional_fields_to_none():
    e = _minimum_valid_evidence()
    assert e["app_id"] is None
    assert e["window_id"] is None


def test_build_coerces_deterministic_matches_to_tuple():
    e = _minimum_valid_evidence(deterministic_matches=["traceback", "modal_error"])
    assert e["deterministic_matches"] == ("traceback", "modal_error")
    assert isinstance(e["deterministic_matches"], tuple)


@pytest.mark.parametrize("verdict", sorted(_VISION_VALID_VERDICTS))
def test_build_accepts_all_valid_verdicts(verdict):
    e = _minimum_valid_evidence(classifier_verdict=verdict)
    assert e["classifier_verdict"] == verdict


@pytest.mark.parametrize("severity", sorted(_VISION_VALID_SEVERITIES))
def test_build_accepts_all_valid_severities(severity):
    e = _minimum_valid_evidence(severity=severity)
    assert e["severity"] == severity


def test_build_accepts_valid_app_id_and_window_id():
    e = _minimum_valid_evidence(app_id="com.apple.Terminal", window_id=12345)
    assert e["app_id"] == "com.apple.Terminal"
    assert e["window_id"] == 12345


def test_build_accepts_zero_confidence_and_one_confidence():
    for c in (0.0, 1.0):
        e = _minimum_valid_evidence(classifier_confidence=c)
        assert e["classifier_confidence"] == c


# ---------------------------------------------------------------------------
# validate_vision_signal_evidence — missing required fields
# ---------------------------------------------------------------------------


def test_validate_rejects_non_dict():
    with pytest.raises(ValueError, match="must be dict"):
        validate_vision_signal_evidence("not a dict")


def test_validate_rejects_none():
    with pytest.raises(ValueError, match="must be dict"):
        validate_vision_signal_evidence(None)


@pytest.mark.parametrize(
    "missing_field",
    [
        "schema_version",
        "frame_hash",
        "frame_ts",
        "frame_path",
        "app_id",
        "window_id",
        "classifier_verdict",
        "classifier_model",
        "classifier_confidence",
        "deterministic_matches",
        "ocr_snippet",
        "severity",
    ],
)
def test_validate_rejects_missing_required_field(missing_field):
    e = dict(_minimum_valid_evidence())
    del e[missing_field]
    with pytest.raises(ValueError, match="missing required"):
        validate_vision_signal_evidence(e)


# ---------------------------------------------------------------------------
# validate_vision_signal_evidence — schema_version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_version", [0, 2, -1, "1", 1.5])
def test_validate_rejects_wrong_schema_version(bad_version):
    e = dict(_minimum_valid_evidence())
    e["schema_version"] = bad_version
    with pytest.raises(ValueError, match="schema_version"):
        validate_vision_signal_evidence(e)


# ---------------------------------------------------------------------------
# validate_vision_signal_evidence — frame_hash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_hash",
    [
        "",                       # empty
        "short",                  # too short
        "0123456789abcdef0",      # too long (17)
        "0123456789abcdeG",       # non-hex
        "0123456789ABCDEF",       # uppercase
        "0123 56789abcdef",       # space
    ],
)
def test_validate_rejects_bad_frame_hash(bad_hash):
    with pytest.raises(ValueError, match="frame_hash"):
        _minimum_valid_evidence(frame_hash=bad_hash)


def test_validate_accepts_all_lowercase_16_hex():
    _minimum_valid_evidence(frame_hash="abcdef0123456789")


# ---------------------------------------------------------------------------
# validate_vision_signal_evidence — frame_ts
# ---------------------------------------------------------------------------


def test_validate_rejects_negative_frame_ts():
    with pytest.raises(ValueError, match="frame_ts"):
        _minimum_valid_evidence(frame_ts=-0.001)


def test_validate_rejects_non_numeric_frame_ts():
    with pytest.raises(ValueError, match="frame_ts"):
        _minimum_valid_evidence(frame_ts="1.0")  # type: ignore[arg-type]


def test_validate_rejects_bool_frame_ts():
    # bool is a subclass of int — must be rejected explicitly
    with pytest.raises(ValueError, match="frame_ts"):
        _minimum_valid_evidence(frame_ts=True)  # type: ignore[arg-type]


def test_validate_accepts_zero_frame_ts():
    e = _minimum_valid_evidence(frame_ts=0.0)
    assert e["frame_ts"] == 0.0


# ---------------------------------------------------------------------------
# validate_vision_signal_evidence — frame_path
# ---------------------------------------------------------------------------


def test_validate_rejects_empty_frame_path():
    with pytest.raises(ValueError, match="frame_path"):
        _minimum_valid_evidence(frame_path="")


def test_validate_rejects_relative_frame_path():
    with pytest.raises(ValueError, match="frame_path"):
        _minimum_valid_evidence(frame_path="relative/path.jpg")


# ---------------------------------------------------------------------------
# validate_vision_signal_evidence — app_id
# ---------------------------------------------------------------------------


def test_validate_rejects_empty_app_id_string():
    with pytest.raises(ValueError, match="app_id"):
        _minimum_valid_evidence(app_id="")


def test_validate_rejects_app_id_length_overflow():
    with pytest.raises(ValueError, match="app_id"):
        _minimum_valid_evidence(app_id="a" * (_VISION_APP_ID_MAX_LEN + 1))


def test_validate_accepts_none_app_id():
    e = _minimum_valid_evidence(app_id=None)
    assert e["app_id"] is None


# ---------------------------------------------------------------------------
# validate_vision_signal_evidence — window_id
# ---------------------------------------------------------------------------


def test_validate_rejects_non_int_window_id():
    with pytest.raises(ValueError, match="window_id"):
        _minimum_valid_evidence(window_id="12345")  # type: ignore[arg-type]


def test_validate_rejects_bool_window_id():
    with pytest.raises(ValueError, match="window_id"):
        _minimum_valid_evidence(window_id=True)  # type: ignore[arg-type]


def test_validate_rejects_negative_window_id():
    with pytest.raises(ValueError, match="window_id"):
        _minimum_valid_evidence(window_id=-1)


def test_validate_accepts_none_window_id():
    e = _minimum_valid_evidence(window_id=None)
    assert e["window_id"] is None


def test_validate_accepts_zero_window_id():
    e = _minimum_valid_evidence(window_id=0)
    assert e["window_id"] == 0


# ---------------------------------------------------------------------------
# validate_vision_signal_evidence — classifier_verdict / severity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "BUG_VISIBLE", "bug", "undefined", "warning"])
def test_validate_rejects_invalid_verdict(bad):
    with pytest.raises(ValueError, match="classifier_verdict"):
        _minimum_valid_evidence(classifier_verdict=bad)


@pytest.mark.parametrize("bad", ["", "CRITICAL", "low", "fatal"])
def test_validate_rejects_invalid_severity(bad):
    with pytest.raises(ValueError, match="severity"):
        _minimum_valid_evidence(severity=bad)


# ---------------------------------------------------------------------------
# validate_vision_signal_evidence — classifier_model / confidence
# ---------------------------------------------------------------------------


def test_validate_rejects_empty_classifier_model():
    with pytest.raises(ValueError, match="classifier_model"):
        _minimum_valid_evidence(classifier_model="")


def test_validate_rejects_classifier_model_overflow():
    with pytest.raises(ValueError, match="classifier_model"):
        _minimum_valid_evidence(
            classifier_model="a" * (_VISION_CLASSIFIER_MODEL_MAX_LEN + 1)
        )


@pytest.mark.parametrize("bad", [-0.001, 1.0001, 2.0, -1.0])
def test_validate_rejects_confidence_out_of_range(bad):
    with pytest.raises(ValueError, match="classifier_confidence"):
        _minimum_valid_evidence(classifier_confidence=bad)


def test_validate_rejects_non_numeric_confidence():
    with pytest.raises(ValueError, match="classifier_confidence"):
        _minimum_valid_evidence(classifier_confidence="0.5")  # type: ignore[arg-type]


def test_validate_rejects_bool_confidence():
    with pytest.raises(ValueError, match="classifier_confidence"):
        _minimum_valid_evidence(classifier_confidence=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# validate_vision_signal_evidence — deterministic_matches
# ---------------------------------------------------------------------------


def test_validate_rejects_non_tuple_deterministic_matches():
    e = dict(_minimum_valid_evidence())
    e["deterministic_matches"] = ["traceback"]  # list, not tuple
    with pytest.raises(ValueError, match="deterministic_matches"):
        validate_vision_signal_evidence(e)


def test_validate_rejects_empty_string_in_deterministic_matches():
    e = dict(_minimum_valid_evidence())
    e["deterministic_matches"] = ("traceback", "")
    with pytest.raises(ValueError, match="deterministic_matches"):
        validate_vision_signal_evidence(e)


def test_validate_rejects_non_string_in_deterministic_matches():
    e = dict(_minimum_valid_evidence())
    e["deterministic_matches"] = ("traceback", 42)
    with pytest.raises(ValueError, match="deterministic_matches"):
        validate_vision_signal_evidence(e)


def test_validate_rejects_deterministic_matches_overflow():
    e = dict(_minimum_valid_evidence())
    e["deterministic_matches"] = tuple(
        f"pat_{i}" for i in range(_VISION_DETERMINISTIC_MATCHES_MAX + 1)
    )
    with pytest.raises(ValueError, match="deterministic_matches"):
        validate_vision_signal_evidence(e)


def test_validate_accepts_empty_deterministic_matches():
    # VLM-only classifier path (Tier 2, no regex hits) is legitimate
    e = _minimum_valid_evidence(deterministic_matches=())
    assert e["deterministic_matches"] == ()


# ---------------------------------------------------------------------------
# validate_vision_signal_evidence — ocr_snippet
# ---------------------------------------------------------------------------


def test_validate_rejects_non_str_ocr_snippet():
    e = dict(_minimum_valid_evidence())
    e["ocr_snippet"] = 42
    with pytest.raises(ValueError, match="ocr_snippet"):
        validate_vision_signal_evidence(e)


def test_validate_rejects_ocr_snippet_overflow():
    with pytest.raises(ValueError, match="ocr_snippet"):
        _minimum_valid_evidence(ocr_snippet="a" * (_VISION_OCR_SNIPPET_MAX_LEN + 1))


def test_validate_accepts_empty_ocr_snippet():
    e = _minimum_valid_evidence(ocr_snippet="")
    assert e["ocr_snippet"] == ""


def test_validate_accepts_exact_max_ocr_snippet():
    e = _minimum_valid_evidence(ocr_snippet="a" * _VISION_OCR_SNIPPET_MAX_LEN)
    assert len(e["ocr_snippet"]) == _VISION_OCR_SNIPPET_MAX_LEN


# ---------------------------------------------------------------------------
# validate_vision_signal_evidence — minimum valid payload survives re-check
# ---------------------------------------------------------------------------


def test_validate_roundtrip_on_built_evidence_is_idempotent():
    # Run validate on output of build — must not raise (already validated once)
    e = _minimum_valid_evidence()
    validate_vision_signal_evidence(e)  # should be silent


# ---------------------------------------------------------------------------
# IntentSignal integration — vision-originated envelope
# ---------------------------------------------------------------------------


def test_intent_signal_accepts_vision_sensor_source_by_enum():
    evidence = _minimum_valid_evidence()
    sig = IntentSignal(
        source=SignalSource.VISION_SENSOR.value,
        target_files=("backend/server.py",),
        repo="jarvis",
        description="vision-detected error",
        evidence={"vision_signal": evidence, "signature": "vision_error"},
        confidence=1.0,
        stable=True,
    )
    assert sig.source == "vision_sensor"
    assert sig.evidence["vision_signal"]["schema_version"] == 1


def test_intent_signal_vision_source_dedup_key_stable():
    # Same frame_hash + signature + repo + files → same dedup_key twice
    evidence = _minimum_valid_evidence()
    make = lambda: IntentSignal(  # noqa: E731
        source=SignalSource.VISION_SENSOR.value,
        target_files=("backend/server.py",),
        repo="jarvis",
        description="first",
        evidence={"vision_signal": evidence, "signature": "vision_error_abc"},
        confidence=1.0,
        stable=True,
    )
    s1 = make()
    s2 = make()
    assert s1.dedup_key == s2.dedup_key


def test_intent_signal_vision_different_signatures_different_dedup():
    base = _minimum_valid_evidence()
    s1 = IntentSignal(
        source="vision_sensor",
        target_files=("backend/x.py",),
        repo="jarvis",
        description="x",
        evidence={"vision_signal": base, "signature": "error_a"},
        confidence=1.0,
        stable=True,
    )
    s2 = IntentSignal(
        source="vision_sensor",
        target_files=("backend/x.py",),
        repo="jarvis",
        description="y",
        evidence={"vision_signal": base, "signature": "error_b"},
        confidence=1.0,
        stable=True,
    )
    assert s1.dedup_key != s2.dedup_key
