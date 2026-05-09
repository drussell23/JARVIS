"""§39 Tier-3 (PRD v2.72 to v2.73, 2026-05-08) -
op trajectory predictor + risk-aware command preview
regression spine.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_tier3(monkeypatch):
    for var in (
        "JARVIS_OP_TRAJECTORY_PREDICTOR_ENABLED",
        "JARVIS_OP_TRAJECTORY_MIN_SAMPLES",
        "JARVIS_OP_TRAJECTORY_HISTORY_LIMIT",
        "JARVIS_RISK_COMMAND_PREVIEW_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    from backend.core.ouroboros.battle_test import (
        op_block_buffer as obb,
    )
    obb.reset_default_buffer_for_tests()
    yield
    obb.reset_default_buffer_for_tests()


# ============================================ Surface #4 — trajectory


def test_trajectory_master_default_false():
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_trajectory_master_truthy(monkeypatch, value):
    monkeypatch.setenv(
        "JARVIS_OP_TRAJECTORY_PREDICTOR_ENABLED", value,
    )
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        master_enabled,
    )
    assert master_enabled() is True


def test_min_samples_clamped(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_TRAJECTORY_MIN_SAMPLES", "999",
    )
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        min_samples,
    )
    assert min_samples() == 50  # clamped MAX
    monkeypatch.setenv(
        "JARVIS_OP_TRAJECTORY_MIN_SAMPLES", "0",
    )
    assert min_samples() == 1  # clamped MIN


def test_history_limit_clamped(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_TRAJECTORY_HISTORY_LIMIT", "9999",
    )
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        history_limit,
    )
    assert history_limit() == 500


# ----- TrajectoryConfidence taxonomy


def test_confidence_taxonomy_4_values():
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        TrajectoryConfidence,
    )
    assert {m.name for m in TrajectoryConfidence} == {
        "HIGH", "MEDIUM", "LOW", "UNKNOWN",
    }


@pytest.mark.parametrize(
    "score,sufficient,expected", [
        (0.95, True, "HIGH"),
        (0.70, True, "HIGH"),
        (0.69, True, "MEDIUM"),
        (0.40, True, "MEDIUM"),
        (0.39, True, "LOW"),
        (0.0, True, "LOW"),
        (0.95, False, "UNKNOWN"),
    ],
)
def test_score_to_confidence(score, sufficient, expected):
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        TrajectoryConfidence, _score_to_confidence,
    )
    result = _score_to_confidence(
        score, sufficient_samples=sufficient,
    )
    assert result is getattr(TrajectoryConfidence, expected)


def test_score_to_confidence_invalid_string():
    """Non-numeric strings raise ValueError → UNKNOWN."""
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        TrajectoryConfidence, _score_to_confidence,
    )
    assert _score_to_confidence(
        "not-a-number", sufficient_samples=True,
    ) is TrajectoryConfidence.UNKNOWN
    assert _score_to_confidence(
        None, sufficient_samples=True,
    ) is TrajectoryConfidence.UNKNOWN


def test_score_to_confidence_nan_falls_through_to_low():
    """``float('nan')`` parses successfully but fails every
    >= comparison (NaN semantics); score falls through to
    LOW. Documenting the actual behavior."""
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        TrajectoryConfidence, _score_to_confidence,
    )
    assert _score_to_confidence(
        "nan", sufficient_samples=True,
    ) is TrajectoryConfidence.LOW


# ----- Percentile


def test_percentile_pure():
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        _percentile,
    )
    assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    assert _percentile([], 0.5) == 0.0
    assert _percentile([1.0], 0.5) == 1.0
    # Out-of-range p clamped
    assert _percentile([1.0, 2.0], 5.0) == 2.0
    assert _percentile([1.0, 2.0], -1.0) == 1.0


# ----- TrajectoryPrediction artifact


def test_trajectory_prediction_to_dict():
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        OP_TRAJECTORY_PREDICTOR_SCHEMA_VERSION,
        TrajectoryConfidence, TrajectoryPrediction,
    )
    p = TrajectoryPrediction(
        op_id="op-123",
        confidence=TrajectoryConfidence.HIGH,
        confidence_score=0.85,
        similar_op_count=10,
        median_duration_s=12.5,
    )
    d = p.to_dict()
    assert d["op_id"] == "op-123"
    assert d["confidence"] == "high"
    assert d["confidence_score"] == 0.85
    assert d["schema_version"] == OP_TRAJECTORY_PREDICTOR_SCHEMA_VERSION


# ----- Predictor


def test_predict_master_off_returns_none():
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        predict_trajectory,
    )
    assert predict_trajectory("op-anything") is None


def test_predict_master_on_no_history(monkeypatch):
    """No committed ops in buffer → UNKNOWN confidence."""
    monkeypatch.setenv(
        "JARVIS_OP_TRAJECTORY_PREDICTOR_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    buf = get_default_buffer()
    buf.start_op("op-active-x")
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        TrajectoryConfidence, predict_trajectory,
    )
    pred = predict_trajectory("op-active-x")
    assert pred is not None
    assert pred.confidence is TrajectoryConfidence.UNKNOWN
    assert pred.similar_op_count == 0


def test_predict_master_on_with_history(monkeypatch):
    """Plant 5 committed ops; predict for active op."""
    monkeypatch.setenv(
        "JARVIS_OP_TRAJECTORY_PREDICTOR_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_OP_TRAJECTORY_MIN_SAMPLES", "3",
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    buf = get_default_buffer()
    for i in range(5):
        buf.start_op(f"op-hist-{i}")
        buf.commit(op_id=f"op-hist-{i}", summary_line=f"x{i}")
    buf.start_op("op-active-y")
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        predict_trajectory,
    )
    pred = predict_trajectory("op-active-y")
    assert pred is not None
    assert pred.similar_op_count == 5


# ----- Renderer


def test_format_master_off():
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        format_trajectory_prediction,
    )
    assert format_trajectory_prediction(None) == ""


def test_format_unknown_confidence(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_TRAJECTORY_PREDICTOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        TrajectoryConfidence, TrajectoryPrediction,
        format_trajectory_prediction,
    )
    p = TrajectoryPrediction(
        op_id="op-1",
        confidence=TrajectoryConfidence.UNKNOWN,
        diagnostic="insufficient_samples:0<3",
    )
    out = format_trajectory_prediction(p)
    assert "unavailable" in out
    assert "⋯" in out


def test_format_high_confidence(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_TRAJECTORY_PREDICTOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        TrajectoryConfidence, TrajectoryPrediction,
        format_trajectory_prediction,
    )
    p = TrajectoryPrediction(
        op_id="op-2",
        confidence=TrajectoryConfidence.HIGH,
        confidence_score=0.85,
        similar_op_count=10,
        median_duration_s=120.0,
        elapsed_so_far_s=30.0,
    )
    out = format_trajectory_prediction(p)
    assert "85%" in out
    assert "🎯" in out
    assert "10 similar ops" in out


# ----- Trajectory AST pins


def _traj_pins():
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _traj_src():
    return Path(
        "backend/core/ouroboros/governance/"
        "op_trajectory_predictor.py"
    ).read_text()


def test_traj_pins_register_5():
    assert len(_traj_pins()) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_traj_pin_passes_canonical(idx):
    pins = _traj_pins()
    src = _traj_src()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_traj_pin_master_default_false_fires():
    pin = next(
        p for p in _traj_pins()
        if "master_default_false" in p.invariant_name
    )
    bad = "def master_enabled():\n    return True\n"
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_traj_pin_authority_asymmetry_fires():
    pin = next(
        p for p in _traj_pins()
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_traj_pin_taxonomy_fires():
    pin = next(
        p for p in _traj_pins()
        if "confidence_taxonomy" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class TrajectoryConfidence(str, enum.Enum):\n"
        "    HIGH = 'high'\n"
    )
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_traj_pin_composes_buffer_fires():
    pin = next(
        p for p in _traj_pins()
        if "composes_canonical_op_block_buffer"
        in p.invariant_name
    )
    bad = "x = 1\n"
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_traj_pin_thresholds_canonical_fires():
    pin = next(
        p for p in _traj_pins()
        if "confidence_thresholds_canonical"
        in p.invariant_name
    )
    bad = "x = 1\n"
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_traj_register_flags_count():
    from backend.core.ouroboros.governance.op_trajectory_predictor import (
        register_flags,
    )

    class _Mock:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _Mock()
    n = register_flags(reg)
    assert n == 3


# ============================================ Surface #19 — preview


def test_preview_master_default_false():
    from backend.core.ouroboros.governance.risk_command_preview import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_preview_master_truthy(monkeypatch, value):
    monkeypatch.setenv(
        "JARVIS_RISK_COMMAND_PREVIEW_ENABLED", value,
    )
    from backend.core.ouroboros.governance.risk_command_preview import (
        master_enabled,
    )
    assert master_enabled() is True


# ----- PreviewVerdict taxonomy + floor map


def test_verdict_taxonomy_4_values():
    from backend.core.ouroboros.governance.risk_command_preview import (
        PreviewVerdict,
    )
    assert {m.name for m in PreviewVerdict} == {
        "SAFE", "NOTIFY", "APPROVAL", "BLOCKED",
    }


@pytest.mark.parametrize(
    "floor,expected", [
        ("safe_auto", "SAFE"),
        ("notify_apply", "NOTIFY"),
        ("approval_required", "APPROVAL"),
        ("blocked", "BLOCKED"),
        ("unknown", "NOTIFY"),  # default
        ("", "NOTIFY"),
    ],
)
def test_verdict_for_floor(floor, expected):
    from backend.core.ouroboros.governance.risk_command_preview import (
        PreviewVerdict, _verdict_for_floor,
    )
    assert _verdict_for_floor(floor) is getattr(
        PreviewVerdict, expected,
    )


def test_verdict_governor_emergency_overrides():
    from backend.core.ouroboros.governance.risk_command_preview import (
        PreviewVerdict, _verdict_for_floor,
    )
    # Even safe_auto becomes BLOCKED under emergency brake.
    assert _verdict_for_floor(
        "safe_auto", governor_emergency=True,
    ) is PreviewVerdict.BLOCKED


# ----- CommandPreview artifact


def test_command_preview_to_dict():
    from backend.core.ouroboros.governance.risk_command_preview import (
        CommandPreview, PreviewVerdict,
        RISK_COMMAND_PREVIEW_SCHEMA_VERSION,
    )
    p = CommandPreview(
        command_summary="test",
        predicted_route="immediate",
        predicted_floor="safe_auto",
        verdict=PreviewVerdict.SAFE,
        estimated_cost_usd=0.03,
    )
    d = p.to_dict()
    assert d["verdict"] == "safe"
    assert d["predicted_route"] == "immediate"
    assert d["schema_version"] == RISK_COMMAND_PREVIEW_SCHEMA_VERSION


# ----- Preview against real canonical sources


def test_preview_master_off_returns_none():
    from backend.core.ouroboros.governance.risk_command_preview import (
        preview_command,
    )
    assert preview_command(
        signal_urgency="high",
        signal_source="test_failure",
    ) is None


def test_preview_high_test_failure_routes_immediate(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_RISK_COMMAND_PREVIEW_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.risk_command_preview import (
        preview_command,
    )
    p = preview_command(
        command_summary="rerun",
        signal_urgency="high",
        signal_source="test_failure",
        task_complexity="moderate",
    )
    assert p is not None
    assert p.predicted_route == "immediate"
    # Cost matches canonical IMMEDIATE estimate.
    assert p.estimated_cost_usd == 0.03


def test_preview_voice_human_is_immediate(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_RISK_COMMAND_PREVIEW_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.risk_command_preview import (
        preview_command,
    )
    p = preview_command(
        signal_urgency="normal",
        signal_source="voice_human",
        task_complexity="moderate",
    )
    assert p.predicted_route == "immediate"


def test_preview_background_source_routes_background(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_RISK_COMMAND_PREVIEW_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.risk_command_preview import (
        preview_command,
    )
    p = preview_command(
        signal_urgency="low",
        signal_source="ai_miner",
        task_complexity="moderate",
    )
    assert p.predicted_route == "background"


def test_preview_heavy_code_routes_complex(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_RISK_COMMAND_PREVIEW_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.risk_command_preview import (
        preview_command,
    )
    p = preview_command(
        signal_urgency="normal",
        signal_source="",
        task_complexity="heavy_code",
    )
    assert p.predicted_route == "complex"


def test_preview_cross_repo_routes_immediate(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_RISK_COMMAND_PREVIEW_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.risk_command_preview import (
        preview_command,
    )
    p = preview_command(
        signal_urgency="normal",
        signal_source="",
        task_complexity="moderate",
        target_files=("a.py", "b.py"),
        cross_repo=True,
    )
    assert p.predicted_route == "immediate"
    assert p.diagnostic == "cross_repo"


# ----- Renderer


def test_preview_format_master_off():
    from backend.core.ouroboros.governance.risk_command_preview import (
        format_command_preview,
    )
    assert format_command_preview(None) == ""


def test_preview_format_renders(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_RISK_COMMAND_PREVIEW_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.risk_command_preview import (
        CommandPreview, PreviewVerdict,
        format_command_preview,
    )
    p = CommandPreview(
        command_summary="test",
        predicted_route="complex",
        predicted_floor="notify_apply",
        verdict=PreviewVerdict.NOTIFY,
        estimated_cost_usd=0.015,
        estimated_duration_s=120.0,
        target_file_count=3,
    )
    out = format_command_preview(p)
    assert "Command preview" in out
    assert "NOTIFY" in out
    assert "complex" in out
    assert "$0.0150" in out


# ----- Preview AST pins


def _preview_pins():
    from backend.core.ouroboros.governance.risk_command_preview import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _preview_src():
    return Path(
        "backend/core/ouroboros/governance/"
        "risk_command_preview.py"
    ).read_text()


def test_preview_pins_register_5():
    assert len(_preview_pins()) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_preview_pin_passes_canonical(idx):
    pins = _preview_pins()
    src = _preview_src()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_preview_pin_master_fires():
    pin = next(
        p for p in _preview_pins()
        if "master_default_false" in p.invariant_name
    )
    bad = "def master_enabled():\n    return True\n"
    assert pin.validate(ast.parse(bad), bad)


def test_preview_pin_authority_fires():
    pin = next(
        p for p in _preview_pins()
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_preview_pin_taxonomy_fires():
    pin = next(
        p for p in _preview_pins()
        if "verdict_taxonomy" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class PreviewVerdict(str, enum.Enum):\n"
        "    SAFE = 'safe'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_preview_pin_urgency_fires():
    pin = next(
        p for p in _preview_pins()
        if "composes_canonical_urgency_router"
        in p.invariant_name
    )
    bad = "x = 1\n"
    assert pin.validate(ast.parse(bad), bad)


def test_preview_pin_floor_fires():
    pin = next(
        p for p in _preview_pins()
        if "composes_canonical_risk_tier_floor"
        in p.invariant_name
    )
    bad = "x = 1\n"
    assert pin.validate(ast.parse(bad), bad)


def test_preview_register_flags_count():
    from backend.core.ouroboros.governance.risk_command_preview import (
        register_flags,
    )

    class _Mock:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _Mock()
    n = register_flags(reg)
    assert n == 1


# ============================================ /forecast REPL


def test_repl_unmatched():
    from backend.core.ouroboros.governance.forecast_repl import (
        dispatch_forecast_command,
    )
    r = dispatch_forecast_command("/something")
    assert r.matched is False


def test_repl_help():
    from backend.core.ouroboros.governance.forecast_repl import (
        dispatch_forecast_command,
    )
    r = dispatch_forecast_command("/forecast help")
    assert r.ok is True
    assert "trajectory" in r.text.lower()
    assert "command" in r.text.lower()


def test_repl_status():
    from backend.core.ouroboros.governance.forecast_repl import (
        dispatch_forecast_command,
    )
    r = dispatch_forecast_command("/forecast status")
    assert r.ok is True


def test_repl_trajectory_master_off():
    from backend.core.ouroboros.governance.forecast_repl import (
        dispatch_forecast_command,
    )
    r = dispatch_forecast_command(
        "/forecast trajectory op-x",
    )
    assert r.ok is False
    assert "disabled" in r.text.lower()


def test_repl_trajectory_no_op_id(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_TRAJECTORY_PREDICTOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.forecast_repl import (
        dispatch_forecast_command,
    )
    r = dispatch_forecast_command("/forecast trajectory")
    assert r.ok is False


def test_repl_trajectory_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_TRAJECTORY_PREDICTOR_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    buf = get_default_buffer()
    buf.start_op("op-test-rerpl")
    from backend.core.ouroboros.governance.forecast_repl import (
        dispatch_forecast_command,
    )
    r = dispatch_forecast_command(
        "/forecast trajectory op-test-rerpl",
    )
    assert r.ok is True


def test_repl_command_master_off():
    from backend.core.ouroboros.governance.forecast_repl import (
        dispatch_forecast_command,
    )
    r = dispatch_forecast_command(
        "/forecast command high test_failure moderate",
    )
    assert r.ok is False


def test_repl_command_too_few_args(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_RISK_COMMAND_PREVIEW_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.forecast_repl import (
        dispatch_forecast_command,
    )
    r = dispatch_forecast_command(
        "/forecast command high test_failure",
    )
    assert r.ok is False


def test_repl_command_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_RISK_COMMAND_PREVIEW_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.forecast_repl import (
        dispatch_forecast_command,
    )
    r = dispatch_forecast_command(
        "/forecast command high test_failure moderate",
    )
    assert r.ok is True
    assert "immediate" in r.text.lower()


def test_repl_command_with_files_and_cross_repo(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_RISK_COMMAND_PREVIEW_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.forecast_repl import (
        dispatch_forecast_command,
    )
    r = dispatch_forecast_command(
        "/forecast command normal voice_human heavy_code "
        "a.py b.py --cross-repo",
    )
    assert r.ok is True


def test_repl_unknown_subcommand():
    from backend.core.ouroboros.governance.forecast_repl import (
        dispatch_forecast_command,
    )
    r = dispatch_forecast_command("/forecast bogus")
    assert r.ok is False


# ----- Canonical-source smokes


def test_canonical_event_trajectory_predicted_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_TRAJECTORY_PREDICTED, _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_TRAJECTORY_PREDICTED == "trajectory_predicted"
    assert EVENT_TYPE_TRAJECTORY_PREDICTED in _VALID_EVENT_TYPES


def test_canonical_event_command_preview_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_COMMAND_PREVIEW_RENDERED, _VALID_EVENT_TYPES,
    )
    assert (
        EVENT_TYPE_COMMAND_PREVIEW_RENDERED
        == "command_preview_rendered"
    )
    assert EVENT_TYPE_COMMAND_PREVIEW_RENDERED in _VALID_EVENT_TYPES


def test_canonical_provider_route_5_values():
    """Lockstep regression — preview's route values are
    canonical ProviderRoute string values."""
    from backend.core.ouroboros.governance.urgency_router import (
        ProviderRoute,
    )
    assert {m.value for m in ProviderRoute} == {
        "immediate", "standard", "complex",
        "background", "speculative",
    }
