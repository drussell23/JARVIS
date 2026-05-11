"""Regression spine for §40 Wave 4 #14 — Mirror-Self Test.

Covers:

* §33.1 cognitive substrate default-FALSE
* Closed 4-value :class:`PredictionDimension` taxonomy
* Closed 4-value :class:`CalibrationVerdict` taxonomy
* record_prediction master-off / master-on / persist-off paths
* record_actual matches predictions by (op_id, dimension)
* Calibration verdict transitions (UNCALIBRATED / POOR / FAIR /
  GOOD) based on accuracy + sample_count
* Time window filtering
* Optional Wave 4 #9 belief_revision_ledger bridge fires on
  falsified prediction (sub-flag controlled)
* Composes cross_process_jsonl for §33.4 persistence
* compute_all_calibrations returns 4 dimensions
* 5 AST pin canonical-source pass + 5 synthetic regressions
* FlagRegistry seeds + master default-FALSE
* SSE event symbol present + in _VALID_EVENT_TYPES
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import mirror_self_test as mst
from backend.core.ouroboros.governance.mirror_self_test import (
    MIRROR_SELF_SCHEMA_VERSION,
    ActualRow,
    CalibrationReport,
    CalibrationVerdict,
    MirrorSelfReport,
    PredictionDimension,
    PredictionRow,
    _ENV_BELIEF_BRIDGE,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MAX_RECORDS,
    _ENV_MIN_SAMPLE,
    _ENV_PERSIST,
    _ENV_WINDOW_S,
    belief_bridge_enabled,
    compute_all_calibrations,
    compute_calibration,
    dimension_glyph,
    format_mirror_self_panel,
    ledger_path,
    master_enabled,
    max_records,
    min_sample_size,
    persistence_enabled,
    record_actual,
    record_prediction,
    register_flags,
    register_shipped_invariants,
    verdict_glyph,
    window_s,
)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER,
        _ENV_PERSIST,
        _ENV_BELIEF_BRIDGE,
        _ENV_MIN_SAMPLE,
        _ENV_WINDOW_S,
        _ENV_MAX_RECORDS,
        _ENV_LEDGER_PATH,
        "JARVIS_BELIEF_REVISION_ENABLED",
        "JARVIS_BELIEF_REVISION_LEDGER_PATH",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_LEDGER_PATH,
        str(tmp_path / "mirror_self.jsonl"),
    )
    monkeypatch.setenv(
        "JARVIS_BELIEF_REVISION_LEDGER_PATH",
        str(tmp_path / "belief.jsonl"),
    )
    yield


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_schema_version():
    assert MIRROR_SELF_SCHEMA_VERSION == "mirror_self.1"


def test_master_default_false():
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert master_enabled() is True


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_belief_bridge_default_true():
    assert belief_bridge_enabled() is True


def test_belief_bridge_explicit_false(monkeypatch):
    monkeypatch.setenv(_ENV_BELIEF_BRIDGE, "false")
    assert belief_bridge_enabled() is False


def test_min_sample_default():
    assert min_sample_size() == 5


def test_min_sample_clamped(monkeypatch):
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "999999")
    assert min_sample_size() == 10_000


def test_window_s_default():
    assert window_s() == 86_400


def test_window_s_clamped_low(monkeypatch):
    monkeypatch.setenv(_ENV_WINDOW_S, "10")
    assert window_s() == 60


def test_max_records_default():
    assert max_records() == 1_000


def test_ledger_path_default(monkeypatch):
    monkeypatch.delenv(_ENV_LEDGER_PATH, raising=False)
    p = ledger_path()
    assert str(p) == ".jarvis/mirror_self_ledger.jsonl"


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


def test_dimension_taxonomy_closed():
    assert {d.value for d in PredictionDimension} == {
        "next_phase", "target_file", "risk_tier", "outcome",
    }


def test_verdict_taxonomy_closed():
    assert {v.value for v in CalibrationVerdict} == {
        "uncalibrated", "poor", "fair", "good",
    }


@pytest.mark.parametrize("d", list(PredictionDimension))
def test_dimension_glyph_known(d):
    assert dimension_glyph(d) != "?"


@pytest.mark.parametrize("v", list(CalibrationVerdict))
def test_verdict_glyph_known(v):
    assert verdict_glyph(v) != "?"


def test_dimension_glyph_unknown():
    assert dimension_glyph("not-a-dim") == "?"


def test_verdict_glyph_unknown():
    assert verdict_glyph("not-a-verdict") == "?"


# ---------------------------------------------------------------------------
# record_prediction
# ---------------------------------------------------------------------------


def test_record_prediction_master_off_no_persist():
    row = record_prediction(
        "op-1", PredictionDimension.NEXT_PHASE, "GENERATE",
        now_unix=1.0,
    )
    assert isinstance(row, PredictionRow)
    assert not ledger_path().exists()


def test_record_prediction_master_on_persists(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    row = record_prediction(
        "op-1", PredictionDimension.NEXT_PHASE, "GENERATE",
        now_unix=1.0,
    )
    assert isinstance(row, PredictionRow)
    p = ledger_path()
    assert p.exists()
    rows = [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["kind"] == "prediction"
    assert rows[0]["dimension"] == "next_phase"
    assert rows[0]["predicted_value"] == "GENERATE"


def test_record_prediction_persist_disabled(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    row = record_prediction(
        "op-1", PredictionDimension.OUTCOME, "success",
        now_unix=1.0,
    )
    assert isinstance(row, PredictionRow)
    assert not ledger_path().exists()


def test_record_prediction_empty_op_id_returns_none(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert record_prediction(
        "", PredictionDimension.NEXT_PHASE, "GENERATE",
    ) is None


def test_record_prediction_empty_value_returns_none(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert record_prediction(
        "op-1", PredictionDimension.NEXT_PHASE, "",
    ) is None


def test_record_prediction_unknown_dimension_returns_none(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert record_prediction(
        "op-1", "not_a_dim", "X",
    ) is None


def test_record_prediction_dimension_from_string(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    row = record_prediction(
        "op-1", "next_phase", "GENERATE", now_unix=1.0,
    )
    assert row is not None
    assert row.dimension is PredictionDimension.NEXT_PHASE


# ---------------------------------------------------------------------------
# record_actual
# ---------------------------------------------------------------------------


def test_record_actual_master_on_correct(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    record_prediction(
        "op-1", PredictionDimension.NEXT_PHASE, "GENERATE",
        now_unix=1.0,
    )
    actual = record_actual(
        "op-1", PredictionDimension.NEXT_PHASE, "GENERATE",
        now_unix=2.0,
    )
    assert isinstance(actual, ActualRow)
    assert actual.was_correct is True


def test_record_actual_master_on_incorrect(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    record_prediction(
        "op-1", PredictionDimension.NEXT_PHASE, "GENERATE",
        now_unix=1.0,
    )
    actual = record_actual(
        "op-1", PredictionDimension.NEXT_PHASE, "VALIDATE",
        now_unix=2.0,
    )
    assert isinstance(actual, ActualRow)
    assert actual.was_correct is False


def test_record_actual_case_insensitive_match(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    record_prediction(
        "op-1", PredictionDimension.NEXT_PHASE, "generate",
        now_unix=1.0,
    )
    actual = record_actual(
        "op-1", PredictionDimension.NEXT_PHASE, "GENERATE",
        now_unix=2.0,
    )
    assert actual is not None
    assert actual.was_correct is True


def test_record_actual_no_prior_prediction_falsified(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    # No prediction recorded — actual still lands but
    # was_correct=False (no prediction to match against)
    actual = record_actual(
        "op-1", PredictionDimension.OUTCOME, "success",
        now_unix=2.0,
    )
    assert actual is not None
    assert actual.was_correct is False


def test_record_actual_empty_op_id_returns_none(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert record_actual(
        "", PredictionDimension.NEXT_PHASE, "X",
    ) is None


# ---------------------------------------------------------------------------
# compute_calibration — verdict transitions
# ---------------------------------------------------------------------------


def _row_for(
    kind: str,
    op_id: str,
    dimension: PredictionDimension,
    value: str,
    *,
    t: float = 1.0,
    correct: bool = False,
) -> dict:
    if kind == "prediction":
        return PredictionRow(
            op_id=op_id,
            dimension=dimension,
            predicted_value=value,
            predicted_at_unix=t,
        ).to_dict()
    return ActualRow(
        op_id=op_id,
        dimension=dimension,
        actual_value=value,
        actual_at_unix=t,
        was_correct=correct,
    ).to_dict()


def test_calibration_master_off_returns_uncalibrated():
    rep = compute_calibration(PredictionDimension.OUTCOME)
    assert rep.verdict is CalibrationVerdict.UNCALIBRATED
    assert rep.sample_count == 0


def test_calibration_under_min_sample_uncalibrated(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "5")
    rows = [
        _row_for(
            "actual", f"op-{i}", PredictionDimension.OUTCOME,
            "success", t=10.0, correct=True,
        )
        for i in range(3)  # only 3 < 5
    ]
    rep = compute_calibration(
        PredictionDimension.OUTCOME, rows=rows, now_unix=100.0,
    )
    assert rep.verdict is CalibrationVerdict.UNCALIBRATED


def test_calibration_good_all_correct(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "5")
    rows = [
        _row_for(
            "actual", f"op-{i}", PredictionDimension.OUTCOME,
            "success", t=10.0, correct=True,
        )
        for i in range(10)
    ]
    rep = compute_calibration(
        PredictionDimension.OUTCOME, rows=rows, now_unix=100.0,
    )
    assert rep.verdict is CalibrationVerdict.GOOD
    assert rep.accuracy == 1.0


def test_calibration_poor_low_accuracy(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "5")
    rows = []
    for i in range(10):
        rows.append(_row_for(
            "actual", f"op-{i}", PredictionDimension.OUTCOME,
            "X", t=10.0, correct=(i < 2),  # 2/10 = 0.20 < 0.40
        ))
    rep = compute_calibration(
        PredictionDimension.OUTCOME, rows=rows, now_unix=100.0,
    )
    assert rep.verdict is CalibrationVerdict.POOR
    assert rep.accuracy == 0.2


def test_calibration_fair_band(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "5")
    rows = []
    for i in range(10):
        rows.append(_row_for(
            "actual", f"op-{i}", PredictionDimension.OUTCOME,
            "X", t=10.0, correct=(i < 6),  # 6/10 = 0.60 → FAIR
        ))
    rep = compute_calibration(
        PredictionDimension.OUTCOME, rows=rows, now_unix=100.0,
    )
    assert rep.verdict is CalibrationVerdict.FAIR


def test_calibration_window_filters_old_rows(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "1")
    # Build 5 rows: 3 inside window, 2 outside
    rows = []
    for i in range(3):
        rows.append(_row_for(
            "actual", f"op-recent-{i}", PredictionDimension.OUTCOME,
            "X", t=100.0, correct=True,
        ))
    for i in range(2):
        rows.append(_row_for(
            "actual", f"op-old-{i}", PredictionDimension.OUTCOME,
            "X", t=1.0, correct=False,  # ancient
        ))
    rep = compute_calibration(
        PredictionDimension.OUTCOME, rows=rows,
        window_seconds=50, now_unix=120.0,
    )
    assert rep.sample_count == 3
    assert rep.accuracy == 1.0


def test_calibration_filters_by_dimension(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "1")
    rows = [
        _row_for(
            "actual", "op-1", PredictionDimension.NEXT_PHASE,
            "X", t=10.0, correct=True,
        ),
        _row_for(
            "actual", "op-2", PredictionDimension.OUTCOME,
            "Y", t=10.0, correct=False,
        ),
    ]
    rep = compute_calibration(
        PredictionDimension.NEXT_PHASE, rows=rows, now_unix=100.0,
    )
    assert rep.sample_count == 1
    assert rep.accuracy == 1.0


# ---------------------------------------------------------------------------
# compute_all_calibrations
# ---------------------------------------------------------------------------


def test_all_calibrations_master_off():
    report = compute_all_calibrations()
    assert isinstance(report, MirrorSelfReport)
    assert report.master_enabled is False
    assert len(report.per_dimension) == 4
    assert all(
        r.verdict is CalibrationVerdict.UNCALIBRATED
        for r in report.per_dimension
    )


def test_all_calibrations_master_on_returns_all_four(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = compute_all_calibrations(rows=[])
    assert report.master_enabled is True
    dims = {r.dimension for r in report.per_dimension}
    assert dims == set(PredictionDimension)


def test_all_calibrations_diagnostic_includes_counts(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = compute_all_calibrations(rows=[])
    assert "uncalibrated=4" in report.diagnostic


# ---------------------------------------------------------------------------
# Belief-revision-ledger bridge
# ---------------------------------------------------------------------------


def test_falsified_prediction_bridges_to_belief_ledger(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_BELIEF_BRIDGE, "true")
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_ENABLED", "true")
    record_prediction(
        "op-X", PredictionDimension.RISK_TIER, "safe_auto",
        now_unix=1.0,
    )
    record_actual(
        "op-X", PredictionDimension.RISK_TIER, "approval_required",
        now_unix=2.0,
    )
    # The belief-revision ledger should now contain an evidence row
    from backend.core.ouroboros.governance import (
        belief_revision_ledger as brl,
    )
    reports = brl.evaluate_recent_beliefs()
    # At least one belief was emitted for the mirror_self_calibration domain
    matching = [
        r for r in reports
        if r.claim and "mirror_self_calibration:risk_tier"
        in (r.claim.domain or "")
    ]
    assert len(matching) >= 1
    assert matching[0].falsifying_count >= 1


def test_bridge_disabled_no_belief_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_BELIEF_BRIDGE, "false")
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_ENABLED", "true")
    record_prediction(
        "op-X", PredictionDimension.RISK_TIER, "safe_auto",
        now_unix=1.0,
    )
    record_actual(
        "op-X", PredictionDimension.RISK_TIER, "approval_required",
        now_unix=2.0,
    )
    from backend.core.ouroboros.governance import (
        belief_revision_ledger as brl,
    )
    reports = brl.evaluate_recent_beliefs()
    matching = [
        r for r in reports
        if r.claim and "mirror_self_calibration"
        in (r.claim.domain or "")
    ]
    assert len(matching) == 0


def test_correct_prediction_no_bridge(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_BELIEF_BRIDGE, "true")
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_ENABLED", "true")
    record_prediction(
        "op-X", PredictionDimension.OUTCOME, "success",
        now_unix=1.0,
    )
    record_actual(
        "op-X", PredictionDimension.OUTCOME, "success",
        now_unix=2.0,
    )
    from backend.core.ouroboros.governance import (
        belief_revision_ledger as brl,
    )
    reports = brl.evaluate_recent_beliefs()
    # No falsifying evidence emitted because prediction was correct
    falsifying = sum(r.falsifying_count for r in reports)
    assert falsifying == 0


# ---------------------------------------------------------------------------
# Full roundtrip with disk persistence
# ---------------------------------------------------------------------------


def test_full_roundtrip_disk_persistence(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    monkeypatch.setenv(_ENV_BELIEF_BRIDGE, "false")
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "3")
    for i in range(5):
        record_prediction(
            f"op-{i}", PredictionDimension.OUTCOME, "success",
            now_unix=10.0 + i,
        )
        record_actual(
            f"op-{i}", PredictionDimension.OUTCOME,
            "success" if i < 4 else "failure",
            now_unix=11.0 + i,
        )
    report = compute_all_calibrations(now_unix=20.0)
    out_dim = next(
        r for r in report.per_dimension
        if r.dimension is PredictionDimension.OUTCOME
    )
    assert out_dim.sample_count == 5
    assert out_dim.correct_count == 4
    assert out_dim.accuracy == 0.8
    assert out_dim.verdict is CalibrationVerdict.GOOD


# ---------------------------------------------------------------------------
# format_mirror_self_panel
# ---------------------------------------------------------------------------


def test_format_panel_master_off():
    out = format_mirror_self_panel()
    assert "disabled" in out


def test_format_panel_master_on_no_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = format_mirror_self_panel()
    assert "no report" in out.lower()


def test_format_panel_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = compute_all_calibrations(rows=[])
    out = format_mirror_self_panel(report)
    assert "Mirror-Self" in out
    # All 4 dimensions should appear
    for d in PredictionDimension:
        assert d.value in out


# ---------------------------------------------------------------------------
# to_dict shape
# ---------------------------------------------------------------------------


def test_prediction_row_to_dict():
    row = PredictionRow(
        op_id="op",
        dimension=PredictionDimension.NEXT_PHASE,
        predicted_value="X",
        predicted_at_unix=1.0,
    )
    d = row.to_dict()
    assert d["kind"] == "prediction"
    assert d["dimension"] == "next_phase"
    assert d["schema_version"] == MIRROR_SELF_SCHEMA_VERSION


def test_actual_row_to_dict():
    row = ActualRow(
        op_id="op",
        dimension=PredictionDimension.OUTCOME,
        actual_value="success",
        actual_at_unix=1.0,
        was_correct=True,
    )
    d = row.to_dict()
    assert d["kind"] == "actual"
    assert d["was_correct"] is True


def test_calibration_report_to_dict():
    rep = CalibrationReport(
        dimension=PredictionDimension.OUTCOME,
        sample_count=10,
        correct_count=8,
        accuracy=0.8,
        verdict=CalibrationVerdict.GOOD,
        window_s=86400,
        diagnostic="x",
    )
    d = rep.to_dict()
    assert d["verdict"] == "good"
    assert d["schema_version"] == MIRROR_SELF_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/mirror_self_test.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 5


@pytest.mark.parametrize(
    "name_part",
    [
        "dimension_taxonomy_closed",
        "verdict_taxonomy_closed",
        "authority_asymmetry",
        "master_default_false",
        "composes_canonical",
    ],
)
def test_pin_canonical_pass(_canonical, name_part):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(p for p in pins if name_part in p.invariant_name)
    assert pin.validate(tree, src) == ()


# Synthetic regressions


def test_pin_dimension_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "dimension_taxonomy_closed" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class PredictionDimension(str, enum.Enum):\n"
        "    NEXT_PHASE = 'next_phase'\n"
        "    TARGET_FILE = 'target_file'\n"
        "    RISK_TIER = 'risk_tier'\n"
        # missing OUTCOME
    )
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


def test_pin_verdict_extra():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy_closed" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class CalibrationVerdict(str, enum.Enum):\n"
        "    UNCALIBRATED = 'uncalibrated'\n"
        "    POOR = 'poor'\n"
        "    FAIR = 'fair'\n"
        "    GOOD = 'good'\n"
        "    EXCELLENT = 'excellent'\n"
    )
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


def test_pin_authority_synthetic():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import x\n"
    )
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


def test_pin_master_synthetic():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    bad_src = (
        "def master_enabled():\n"
        "    return _flag('JARVIS_X', default=True)\n"
    )
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


def test_pin_composes_synthetic_missing():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    bad_src = "# no canonical composition\n"
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self):
        self.registered: List[Any] = []

    def register(self, spec):
        self.registered.append(spec)


def test_flag_registry_seed_count():
    reg = _FakeRegistry()
    count = register_flags(reg)
    assert count == 6
    names = {spec.name for spec in reg.registered}
    expected = {
        _ENV_MASTER,
        _ENV_PERSIST,
        _ENV_BELIEF_BRIDGE,
        _ENV_MIN_SAMPLE,
        _ENV_WINDOW_S,
        _ENV_MAX_RECORDS,
    }
    assert expected.issubset(names)


def test_flag_master_default_false():
    reg = _FakeRegistry()
    register_flags(reg)
    master = next(s for s in reg.registered if s.name == _ENV_MASTER)
    assert master.default is False


# ---------------------------------------------------------------------------
# SSE bind
# ---------------------------------------------------------------------------


def test_sse_event_symbol_exists():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert hasattr(ios, "EVENT_TYPE_MIRROR_SELF_CALIBRATED")
    assert (
        ios.EVENT_TYPE_MIRROR_SELF_CALIBRATED
        == "mirror_self_calibrated"
    )


def test_sse_event_in_valid_set():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert "mirror_self_calibrated" in ios._VALID_EVENT_TYPES
