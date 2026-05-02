"""Priority #5 Slice 1 — CIGW primitive regression suite.

Tests the pure-stdlib primitive layer: 3 closed-taxonomy 5-value
enums, 4 frozen dataclasses with to_dict/from_dict round-trip, 5
env-knob helpers with floor+ceiling clamps, 4 pure decision
functions (compute_baseline_mean + compute_severity +
compute_gradient_reading + compute_gradient_outcome).

Test classes:
  * TestMasterFlag — asymmetric env semantics
  * TestEnvKnobs — clamping discipline
  * TestClosedTaxonomies — 5-value enum integrity
  * TestSchemaIntegrity — frozen dataclasses + round-trip + schema mismatch
  * TestComputeBaselineMean — boundary conditions
  * TestComputeSeverity — closed-taxonomy step function
  * TestComputeGradientReading — per-target reading construction
  * TestComputeGradientOutcome — closed-taxonomy outcome resolution
  * TestReadingHelpers — is_breach + is_drift
  * TestDefensiveContract — public surface NEVER raises
  * TestCostContractAuthorityInvariants — AST-level pin
"""
from __future__ import annotations

import ast
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance.verification import (
    gradient_watcher as gw_mod,
)
from backend.core.ouroboros.governance.verification.gradient_watcher import (
    CIGW_SCHEMA_VERSION,
    GradientBreach,
    GradientOutcome,
    GradientReading,
    GradientReport,
    GradientSeverity,
    InvariantSample,
    MeasurementKind,
    cigw_critical_threshold_pct,
    cigw_enabled,
    cigw_high_threshold_pct,
    cigw_low_threshold_pct,
    cigw_medium_threshold_pct,
    cigw_rolling_window_size,
    compute_baseline_mean,
    compute_gradient_outcome,
    compute_gradient_reading,
    compute_severity,
)


# ---------------------------------------------------------------------------
# Forbidden-call tokens
# ---------------------------------------------------------------------------

_FORBIDDEN_CALL_TOKENS = (
    "e" + "val(",
    "e" + "xec(",
    "comp" + "ile(",
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _sample(
    target_id: str = "f.py",
    kind: MeasurementKind = MeasurementKind.LINE_COUNT,
    value: float = 100.0,
) -> InvariantSample:
    return InvariantSample(
        target_id=target_id,
        measurement_kind=kind,
        value=value,
    )


def _reading(
    target_id: str = "f.py",
    kind: MeasurementKind = MeasurementKind.LINE_COUNT,
    severity: GradientSeverity = GradientSeverity.NONE,
    delta_pct: float = 0.0,
) -> GradientReading:
    return GradientReading(
        target_id=target_id,
        measurement_kind=kind,
        baseline_mean=100.0,
        current_value=100.0 + delta_pct,
        delta_abs=delta_pct,
        delta_pct=delta_pct,
        severity=severity,
    )


# ---------------------------------------------------------------------------
# TestMasterFlag
# ---------------------------------------------------------------------------


class TestMasterFlag:

    def test_default_is_false(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CIGW_ENABLED", raising=False)
        assert cigw_enabled() is False

    def test_empty_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CIGW_ENABLED", "")
        assert cigw_enabled() is False

    @pytest.mark.parametrize("v", ["1", "true", "TRUE", "yes", "ON"])
    def test_truthy(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_CIGW_ENABLED", v)
        assert cigw_enabled() is True

    @pytest.mark.parametrize("v", ["0", "false", "no", "off"])
    def test_falsy(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_CIGW_ENABLED", v)
        assert cigw_enabled() is False

    @pytest.mark.parametrize("v", ["", "   ", "\t\n"])
    def test_whitespace_treated_as_unset(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_CIGW_ENABLED", v)
        assert cigw_enabled() is False


# ---------------------------------------------------------------------------
# TestEnvKnobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:

    def test_window_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CIGW_ROLLING_WINDOW", raising=False)
        assert cigw_rolling_window_size() == 50

    def test_window_clamps(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CIGW_ROLLING_WINDOW", "0")
        assert cigw_rolling_window_size() == 10
        monkeypatch.setenv("JARVIS_CIGW_ROLLING_WINDOW", "999999")
        assert cigw_rolling_window_size() == 1000

    def test_window_garbage(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CIGW_ROLLING_WINDOW", "junk")
        assert cigw_rolling_window_size() == 50

    def test_low_threshold_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CIGW_LOW_THRESHOLD_PCT", raising=False)
        assert cigw_low_threshold_pct() == pytest.approx(5.0)

    def test_low_threshold_clamps(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CIGW_LOW_THRESHOLD_PCT", "-1")
        assert cigw_low_threshold_pct() == pytest.approx(0.0)
        monkeypatch.setenv("JARVIS_CIGW_LOW_THRESHOLD_PCT", "9999")
        assert cigw_low_threshold_pct() == pytest.approx(100.0)

    def test_medium_threshold_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CIGW_MEDIUM_THRESHOLD_PCT", raising=False)
        assert cigw_medium_threshold_pct() == pytest.approx(15.0)

    def test_high_threshold_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CIGW_HIGH_THRESHOLD_PCT", raising=False)
        assert cigw_high_threshold_pct() == pytest.approx(30.0)

    def test_critical_threshold_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CIGW_CRITICAL_THRESHOLD_PCT", raising=False)
        assert cigw_critical_threshold_pct() == pytest.approx(50.0)

    def test_critical_threshold_above_100(self, monkeypatch):
        """Critical can exceed 100% because integer metrics like
        line count can shift 200%+ when a file is rewritten."""
        monkeypatch.setenv("JARVIS_CIGW_CRITICAL_THRESHOLD_PCT", "500")
        assert cigw_critical_threshold_pct() == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# TestClosedTaxonomies
# ---------------------------------------------------------------------------


class TestClosedTaxonomies:

    def test_measurement_kind_5_values(self):
        assert {x.value for x in MeasurementKind} == {
            "line_count", "function_count", "import_count",
            "banned_token_count", "branch_complexity",
        }

    def test_severity_5_values(self):
        assert {x.value for x in GradientSeverity} == {
            "none", "low", "medium", "high", "critical",
        }

    def test_outcome_5_values(self):
        assert {x.value for x in GradientOutcome} == {
            "stable", "drifting", "breached", "disabled", "failed",
        }

    def test_all_string_subclassed(self):
        for enum_cls in (MeasurementKind, GradientSeverity, GradientOutcome):
            for member in enum_cls:
                assert isinstance(member.value, str)


# ---------------------------------------------------------------------------
# TestSchemaIntegrity
# ---------------------------------------------------------------------------


class TestSchemaIntegrity:

    def test_invariant_sample_frozen(self):
        s = _sample()
        with pytest.raises(FrozenInstanceError):
            s.value = 200.0  # type: ignore

    def test_invariant_sample_round_trip(self):
        s = _sample(target_id="x.py", value=42.0)
        recon = InvariantSample.from_dict(s.to_dict())
        assert recon is not None
        assert recon.target_id == "x.py"
        assert recon.value == pytest.approx(42.0)
        assert recon.measurement_kind is MeasurementKind.LINE_COUNT

    def test_invariant_sample_schema_mismatch(self):
        s = _sample()
        d = s.to_dict()
        d["schema_version"] = "wrong.99"
        assert InvariantSample.from_dict(d) is None

    def test_invariant_sample_unknown_kind(self):
        s = _sample()
        d = s.to_dict()
        d["measurement_kind"] = "made_up_kind"
        assert InvariantSample.from_dict(d) is None

    def test_invariant_sample_garbage(self):
        assert InvariantSample.from_dict("not a dict") is None  # type: ignore
        assert InvariantSample.from_dict({}) is None
        assert InvariantSample.from_dict(None) is None  # type: ignore

    def test_invariant_sample_detail_truncated(self):
        s = InvariantSample(
            target_id="x", measurement_kind=MeasurementKind.LINE_COUNT,
            value=1.0, detail="A" * 1000,
        )
        d = s.to_dict()
        assert len(d["detail"]) == 256

    def test_gradient_reading_frozen(self):
        r = _reading()
        with pytest.raises(FrozenInstanceError):
            r.delta_pct = 99.0  # type: ignore

    def test_gradient_breach_to_dict(self):
        r = _reading(severity=GradientSeverity.HIGH, delta_pct=35.0)
        b = GradientBreach(reading=r, detail="x")
        d = b.to_dict()
        assert "reading" in d
        assert d["detail"] == "x"

    def test_gradient_report_frozen(self):
        r = GradientReport(outcome=GradientOutcome.STABLE)
        with pytest.raises(FrozenInstanceError):
            r.detail = "x"  # type: ignore

    def test_gradient_report_to_dict_shape(self):
        r = GradientReport(
            outcome=GradientOutcome.BREACHED,
            readings=(_reading(severity=GradientSeverity.CRITICAL),),
            breaches=(GradientBreach(
                reading=_reading(severity=GradientSeverity.CRITICAL),
            ),),
            total_samples=5,
        )
        d = r.to_dict()
        assert d["outcome"] == "breached"
        assert len(d["readings"]) == 1
        assert len(d["breaches"]) == 1
        assert d["total_samples"] == 5


# ---------------------------------------------------------------------------
# TestComputeBaselineMean
# ---------------------------------------------------------------------------


class TestComputeBaselineMean:

    def test_empty(self):
        assert compute_baseline_mean([]) == 0.0

    def test_single_sample_excludes_last(self):
        # Only sample IS last → exclusion would empty the list → 0.0
        # Implementation: with len==1 + exclude_last, returns the one
        # value (since we don't exclude when only 1 is present).
        s = _sample(value=100.0)
        assert compute_baseline_mean([s], exclude_last=True) == 100.0

    def test_single_sample_includes(self):
        s = _sample(value=100.0)
        assert compute_baseline_mean([s], exclude_last=False) == 100.0

    def test_multiple_excludes_last(self):
        samples = [_sample(value=v) for v in [100, 105, 110, 115, 200]]
        # Mean of first 4 = 107.5
        assert compute_baseline_mean(samples) == pytest.approx(107.5)

    def test_multiple_includes(self):
        samples = [_sample(value=v) for v in [100, 105, 110, 115, 200]]
        # Mean of all 5 = 126.0
        assert compute_baseline_mean(samples, exclude_last=False) == pytest.approx(126.0)

    def test_garbage_filtered(self):
        samples = ["bad", _sample(value=10.0), 42, _sample(value=20.0)]
        # Only 2 valid; exclude_last → 1 → mean = 10.0
        assert compute_baseline_mean(samples) == pytest.approx(10.0)  # type: ignore

    def test_never_raises_on_garbage(self):
        result = compute_baseline_mean(None)  # type: ignore
        assert result == 0.0


# ---------------------------------------------------------------------------
# TestComputeSeverity
# ---------------------------------------------------------------------------


class TestComputeSeverity:

    @pytest.mark.parametrize("d,expected", [
        (0.0, GradientSeverity.NONE),
        (4.99, GradientSeverity.NONE),
        (5.0, GradientSeverity.LOW),
        (10.0, GradientSeverity.LOW),
        (14.99, GradientSeverity.LOW),
        (15.0, GradientSeverity.MEDIUM),
        (25.0, GradientSeverity.MEDIUM),
        (29.99, GradientSeverity.MEDIUM),
        (30.0, GradientSeverity.HIGH),
        (40.0, GradientSeverity.HIGH),
        (49.99, GradientSeverity.HIGH),
        (50.0, GradientSeverity.CRITICAL),
        (200.0, GradientSeverity.CRITICAL),
    ])
    def test_step_function(self, d, expected):
        assert compute_severity(d) is expected

    def test_negative_treated_as_abs(self):
        assert compute_severity(-50.0) is GradientSeverity.CRITICAL

    def test_nan_returns_none(self):
        assert compute_severity(float("nan")) is GradientSeverity.NONE

    def test_inf_returns_none(self):
        assert compute_severity(float("inf")) is GradientSeverity.NONE

    def test_explicit_thresholds_override(self):
        # Override low threshold to 10; 7 is below → NONE
        assert compute_severity(
            7.0, low_threshold=10.0,
        ) is GradientSeverity.NONE

    def test_reversed_thresholds_resolve_gracefully(self):
        # Reversed: low > medium > high > critical (all under 5)
        # The function sorts ascending and walks; a value of 50 is
        # above ALL of them → resolves to highest severity it
        # crosses, which is whatever sits last in the sorted order.
        result = compute_severity(
            50.0,
            low_threshold=40.0,
            medium_threshold=30.0,
            high_threshold=20.0,
            critical_threshold=10.0,
        )
        # All four thresholds crossed; the function resolves
        # using sorted ascending → last severity assigned wins
        # (highest threshold value crossed).
        assert isinstance(result, GradientSeverity)


# ---------------------------------------------------------------------------
# TestComputeGradientReading
# ---------------------------------------------------------------------------


class TestComputeGradientReading:

    def test_empty_returns_none(self):
        assert compute_gradient_reading([]) is None

    def test_single_sample_returns_none(self):
        assert compute_gradient_reading([_sample()]) is None

    def test_homogeneous_baseline(self):
        samples = [_sample(value=v) for v in [100.0, 100.0, 100.0, 102.0]]
        r = compute_gradient_reading(samples)
        assert r is not None
        assert r.baseline_mean == pytest.approx(100.0)
        assert r.current_value == pytest.approx(102.0)
        assert r.delta_pct == pytest.approx(2.0)
        assert r.severity is GradientSeverity.NONE

    def test_high_drift(self):
        samples = [_sample(value=v) for v in [100.0, 100.0, 100.0, 140.0]]
        r = compute_gradient_reading(samples)
        assert r is not None
        assert r.delta_pct == pytest.approx(40.0)
        assert r.severity is GradientSeverity.HIGH

    def test_zero_baseline_to_nonzero_critical(self):
        samples = [
            _sample(value=0.0, kind=MeasurementKind.BANNED_TOKEN_COUNT),
            _sample(value=0.0, kind=MeasurementKind.BANNED_TOKEN_COUNT),
            _sample(value=1.0, kind=MeasurementKind.BANNED_TOKEN_COUNT),
        ]
        r = compute_gradient_reading(samples)
        assert r is not None
        assert r.severity is GradientSeverity.CRITICAL

    def test_zero_baseline_zero_current(self):
        samples = [
            _sample(value=0.0, kind=MeasurementKind.BANNED_TOKEN_COUNT),
            _sample(value=0.0, kind=MeasurementKind.BANNED_TOKEN_COUNT),
            _sample(value=0.0, kind=MeasurementKind.BANNED_TOKEN_COUNT),
        ]
        r = compute_gradient_reading(samples)
        assert r is not None
        assert r.delta_pct == pytest.approx(0.0)
        assert r.severity is GradientSeverity.NONE

    def test_heterogeneous_target_returns_none(self):
        samples = [
            _sample(target_id="a.py", value=100.0),
            _sample(target_id="b.py", value=200.0),
        ]
        # Two samples but different targets → only 1 matches the
        # latest target → insufficient → None
        assert compute_gradient_reading(samples) is None

    def test_heterogeneous_kind_returns_none(self):
        samples = [
            _sample(kind=MeasurementKind.LINE_COUNT, value=100.0),
            _sample(kind=MeasurementKind.FUNCTION_COUNT, value=200.0),
        ]
        assert compute_gradient_reading(samples) is None

    def test_garbage_returns_none(self):
        assert compute_gradient_reading(None) is None  # type: ignore
        assert compute_gradient_reading("string") is None  # type: ignore


# ---------------------------------------------------------------------------
# TestComputeGradientOutcome
# ---------------------------------------------------------------------------


class TestComputeGradientOutcome:

    def test_master_off_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CIGW_ENABLED", "false")
        report = compute_gradient_outcome([_reading()])
        assert report.outcome is GradientOutcome.DISABLED

    def test_enabled_override_false(self):
        report = compute_gradient_outcome(
            [_reading()], enabled_override=False,
        )
        assert report.outcome is GradientOutcome.DISABLED

    def test_enabled_override_true_engages(self):
        report = compute_gradient_outcome(
            [_reading()], enabled_override=True,
        )
        assert report.outcome is GradientOutcome.STABLE

    def test_string_input_failed(self):
        report = compute_gradient_outcome(
            "not a sequence", enabled_override=True,  # type: ignore
        )
        assert report.outcome is GradientOutcome.FAILED
        assert "string_like_input" in report.detail

    def test_bytes_input_failed(self):
        report = compute_gradient_outcome(
            b"\x00", enabled_override=True,  # type: ignore
        )
        assert report.outcome is GradientOutcome.FAILED

    def test_none_input_failed(self):
        report = compute_gradient_outcome(
            None, enabled_override=True,  # type: ignore
        )
        # None fails the Sequence check
        assert report.outcome is GradientOutcome.FAILED

    def test_empty_list_stable(self):
        report = compute_gradient_outcome([], enabled_override=True)
        assert report.outcome is GradientOutcome.STABLE
        assert "no_readings" in report.detail

    def test_all_none_severity_stable(self):
        readings = [
            _reading(severity=GradientSeverity.NONE),
            _reading(severity=GradientSeverity.NONE),
        ]
        report = compute_gradient_outcome(readings, enabled_override=True)
        assert report.outcome is GradientOutcome.STABLE

    def test_low_severity_drifting(self):
        readings = [
            _reading(severity=GradientSeverity.NONE),
            _reading(severity=GradientSeverity.LOW, delta_pct=10.0),
        ]
        report = compute_gradient_outcome(readings, enabled_override=True)
        assert report.outcome is GradientOutcome.DRIFTING

    def test_medium_severity_drifting(self):
        readings = [_reading(severity=GradientSeverity.MEDIUM, delta_pct=20.0)]
        report = compute_gradient_outcome(readings, enabled_override=True)
        assert report.outcome is GradientOutcome.DRIFTING

    def test_high_severity_breached(self):
        readings = [_reading(severity=GradientSeverity.HIGH, delta_pct=35.0)]
        report = compute_gradient_outcome(readings, enabled_override=True)
        assert report.outcome is GradientOutcome.BREACHED
        assert len(report.breaches) == 1

    def test_critical_severity_breached(self):
        readings = [_reading(severity=GradientSeverity.CRITICAL, delta_pct=100.0)]
        report = compute_gradient_outcome(readings, enabled_override=True)
        assert report.outcome is GradientOutcome.BREACHED

    def test_breach_takes_precedence_over_drift(self):
        readings = [
            _reading(severity=GradientSeverity.LOW),
            _reading(severity=GradientSeverity.MEDIUM),
            _reading(severity=GradientSeverity.HIGH),
        ]
        report = compute_gradient_outcome(readings, enabled_override=True)
        assert report.outcome is GradientOutcome.BREACHED
        assert len(report.breaches) == 1

    def test_garbage_items_filtered(self):
        readings = ["bad", _reading(), 42]
        report = compute_gradient_outcome(readings, enabled_override=True)  # type: ignore
        # 1 valid reading with NONE severity → STABLE
        assert report.outcome is GradientOutcome.STABLE
        assert len(report.readings) == 1

    def test_total_samples_accumulated(self):
        readings = [
            GradientReading(
                target_id="x", measurement_kind=MeasurementKind.LINE_COUNT,
                baseline_mean=100.0, current_value=100.0,
                delta_abs=0.0, delta_pct=0.0,
                severity=GradientSeverity.NONE,
                sample_count=10,
            ),
            GradientReading(
                target_id="y", measurement_kind=MeasurementKind.LINE_COUNT,
                baseline_mean=200.0, current_value=200.0,
                delta_abs=0.0, delta_pct=0.0,
                severity=GradientSeverity.NONE,
                sample_count=20,
            ),
        ]
        report = compute_gradient_outcome(readings, enabled_override=True)
        assert report.total_samples == 30

    def test_detail_token_shape(self):
        readings = [
            _reading(severity=GradientSeverity.HIGH, delta_pct=35.0),
            _reading(severity=GradientSeverity.NONE),
        ]
        report = compute_gradient_outcome(readings, enabled_override=True)
        for token in (
            "outcome=breached", "readings=2", "breaches=1",
            "high=1", "none=1",
        ):
            assert token in report.detail


# ---------------------------------------------------------------------------
# TestReadingHelpers
# ---------------------------------------------------------------------------


class TestReadingHelpers:

    @pytest.mark.parametrize("severity,is_breach,is_drift", [
        (GradientSeverity.NONE, False, False),
        (GradientSeverity.LOW, False, True),
        (GradientSeverity.MEDIUM, False, True),
        (GradientSeverity.HIGH, True, False),
        (GradientSeverity.CRITICAL, True, False),
    ])
    def test_severity_classification(self, severity, is_breach, is_drift):
        r = _reading(severity=severity)
        assert r.is_breach() is is_breach
        assert r.is_drift() is is_drift


# ---------------------------------------------------------------------------
# TestDefensiveContract
# ---------------------------------------------------------------------------


class TestDefensiveContract:

    def test_compute_severity_never_raises(self):
        for inp in (None, "string", float("nan"), float("inf"), -1e10):
            result = compute_severity(inp)  # type: ignore
            assert isinstance(result, GradientSeverity)

    def test_compute_baseline_mean_never_raises(self):
        for inp in (None, "string", 42, []):
            result = compute_baseline_mean(inp)  # type: ignore
            assert result == 0.0

    def test_compute_gradient_reading_never_raises(self):
        for inp in (None, "string", 42):
            result = compute_gradient_reading(inp)  # type: ignore
            assert result is None

    def test_compute_gradient_outcome_never_raises(self):
        for inp in (None, "string", 42, b"bytes"):
            result = compute_gradient_outcome(
                inp, enabled_override=True,  # type: ignore
            )
            assert isinstance(result, GradientReport)


# ---------------------------------------------------------------------------
# TestCostContractAuthorityInvariants — AST-level pin
# ---------------------------------------------------------------------------


_GW_PATH = Path(gw_mod.__file__)


def _module_source() -> str:
    return _GW_PATH.read_text()


def _module_ast() -> ast.AST:
    return ast.parse(_module_source())


_BANNED_IMPORT_SUBSTRINGS = (
    ".providers", "doubleword_provider", "urgency_router",
    "candidate_generator", "orchestrator", "tool_executor",
    "phase_runner", "iron_gate", "change_engine",
    "auto_action_router", "subagent_scheduler",
    "semantic_guardian", "semantic_firewall", "risk_engine",
)


class TestCostContractAuthorityInvariants:

    def test_no_banned_imports(self):
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for banned in _BANNED_IMPORT_SUBSTRINGS:
                        assert banned not in alias.name
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for banned in _BANNED_IMPORT_SUBSTRINGS:
                    assert banned not in module

    def test_no_governance_imports_pure_stdlib(self):
        """Slice 1 primitive MUST be pure-stdlib — strongest
        authority invariant. Mirrors Priority #1/#2/#3/#4 Slice 1
        discipline."""
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "backend." not in module, (
                    f"primitive must be pure-stdlib — found {module!r}"
                )
                assert "governance" not in module, (
                    f"primitive must be pure-stdlib — found {module!r}"
                )

    def test_no_eval_family_calls(self):
        src = _module_source()
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Name,
            ):
                assert node.func.id not in ("exec", "eval", "compile")
        for token in _FORBIDDEN_CALL_TOKENS:
            assert token not in src

    def test_no_subprocess_or_os_system(self):
        src = _module_source()
        assert "subprocess" not in src
        assert "os." + "system" not in src

    def test_no_async_functions(self):
        """Slice 1 is sync; Slice 2 wraps via to_thread."""
        tree = _module_ast()
        for node in ast.walk(tree):
            assert not isinstance(node, ast.AsyncFunctionDef), (
                f"forbidden async function: "
                f"{getattr(node, 'name', '?')}"
            )

    def test_no_mutation_calls(self):
        tree = _module_ast()
        forbidden = {
            ("shutil", "rmtree"), ("os", "remove"), ("os", "unlink"),
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Attribute,
            ):
                if isinstance(node.func.value, ast.Name):
                    pair = (node.func.value.id, node.func.attr)
                    assert pair not in forbidden

    def test_public_api_exported(self):
        for name in gw_mod.__all__:
            assert hasattr(gw_mod, name), (
                f"gw_mod.__all__ contains '{name}' which is not "
                f"a module attribute"
            )

    def test_cost_contract_constant_present(self):
        assert hasattr(
            gw_mod, "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
        )
        assert gw_mod.COST_CONTRACT_PRESERVED_BY_CONSTRUCTION is True

    def test_schema_version_constant(self):
        assert CIGW_SCHEMA_VERSION == "gradient_watcher.1"
