"""Regression spine for §40 Wave 4 #12 — Schelling-Point
Consensus Prior.

Covers:

* §33.1 cognitive substrate default-FALSE
* Closed 4-value :class:`SchellingDecision` taxonomy
* Closed 4-value :class:`PriorTrustLevel` taxonomy
* Composes ``cross_process_jsonl.flock_append_line`` for §33.4
  history persistence
* Composes Move 6.5 ``generative_quorum.ConsensusVerdict``
  (read-only via .outcome.value access)
* record_prior_outcome master-off / master-on behavior
* compute_prior_trust aggregation + trust-level thresholds
* break_tie 4-value decision matrix (NO_TIE / TIE_BROKEN /
  NO_RECORD / DISABLED)
* 5 AST pin canonical-source pass + 5 synthetic regressions
* FlagRegistry seeds + master default-FALSE
* SSE event symbol present + in valid set
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Tuple

import pytest


from backend.core.ouroboros.governance import (
    schelling_consensus_prior as scp,
)
from backend.core.ouroboros.governance.schelling_consensus_prior import (
    SCHELLING_PRIOR_SCHEMA_VERSION,
    PriorOutcomeRecord,
    PriorTrustLevel,
    PriorTrustReport,
    SchellingDecision,
    SchellingTieBreakReport,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MAX_RECORDS,
    _ENV_MIN_SAMPLE,
    _ENV_PERSIST,
    break_tie,
    compute_prior_trust,
    decision_glyph,
    format_tiebreak_panel,
    ledger_path,
    master_enabled,
    max_records,
    min_sample_size,
    persistence_enabled,
    record_prior_outcome,
    register_flags,
    register_shipped_invariants,
    trust_glyph,
)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER,
        _ENV_PERSIST,
        _ENV_MAX_RECORDS,
        _ENV_MIN_SAMPLE,
        _ENV_LEDGER_PATH,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_LEDGER_PATH,
        str(tmp_path / "history.jsonl"),
    )
    yield


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_schema_version():
    assert SCHELLING_PRIOR_SCHEMA_VERSION == "schelling_prior.1"


def test_master_default_false():
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert master_enabled() is True


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_max_records_default():
    assert max_records() == 500


def test_max_records_clamped(monkeypatch):
    monkeypatch.setenv(_ENV_MAX_RECORDS, "999999999")
    assert max_records() == 100_000


def test_min_sample_default():
    assert min_sample_size() == 3


def test_min_sample_clamped_low(monkeypatch):
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "0")
    assert min_sample_size() == 1


def test_ledger_path_default_relative(monkeypatch):
    monkeypatch.delenv(_ENV_LEDGER_PATH, raising=False)
    p = ledger_path()
    assert str(p) == ".jarvis/schelling_prior_history.jsonl"


def test_ledger_path_env_override(monkeypatch, tmp_path):
    target = tmp_path / "alt.jsonl"
    monkeypatch.setenv(_ENV_LEDGER_PATH, str(target))
    assert ledger_path() == target


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


def test_decision_taxonomy_closed():
    assert {d.value for d in SchellingDecision} == {
        "no_tie", "tie_broken", "no_record", "disabled",
    }


def test_trust_taxonomy_closed():
    assert {t.value for t in PriorTrustLevel} == {
        "unknown", "low", "medium", "high",
    }


@pytest.mark.parametrize(
    "decision",
    list(SchellingDecision),
)
def test_decision_glyph_known(decision):
    assert decision_glyph(decision) != "?"


def test_decision_glyph_unknown():
    assert decision_glyph("not-a-decision") == "?"


@pytest.mark.parametrize(
    "trust",
    list(PriorTrustLevel),
)
def test_trust_glyph_known(trust):
    assert trust_glyph(trust) != "?"


# ---------------------------------------------------------------------------
# record_prior_outcome
# ---------------------------------------------------------------------------


def test_record_master_off_no_persist():
    rec = record_prior_outcome(
        "Stability", "op-1", "sig-1", True, now_unix=1.0,
    )
    assert isinstance(rec, PriorOutcomeRecord)
    assert not ledger_path().exists()


def test_record_master_on_persists(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    rec = record_prior_outcome(
        "Stability", "op-1", "sig-1", True, now_unix=1.0,
    )
    assert isinstance(rec, PriorOutcomeRecord)
    p = ledger_path()
    assert p.exists()
    rows = [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["kind"] == "prior_outcome"
    assert rows[0]["prior_kind"] == "Stability"
    assert rows[0]["was_accepted"] is True


def test_record_persist_disabled(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    rec = record_prior_outcome(
        "Stability", "op-1", "sig", True, now_unix=1.0,
    )
    assert isinstance(rec, PriorOutcomeRecord)
    assert not ledger_path().exists()


def test_record_empty_prior_kind_returns_none(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert record_prior_outcome("", "op-1", "sig", True) is None


def test_record_truncates_oversized_fields(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    rec = record_prior_outcome(
        "X" * 1000, "op-1", "sig", True, now_unix=1.0,
    )
    assert rec is not None
    assert len(rec.to_dict()["prior_kind"]) <= 64


# ---------------------------------------------------------------------------
# compute_prior_trust
# ---------------------------------------------------------------------------


def _row(prior_kind: str, accepted: bool, op_id: str = "op") -> dict:
    return PriorOutcomeRecord(
        prior_kind=prior_kind,
        op_id=op_id,
        ast_signature="sig",
        was_accepted=accepted,
        observed_at_unix=1.0,
    ).to_dict()


def test_trust_unknown_when_under_sample(monkeypatch):
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "3")
    rows = [_row("A", True), _row("A", True)]  # only 2 samples
    rep = compute_prior_trust("A", rows=rows)
    assert rep.trust_level is PriorTrustLevel.UNKNOWN
    assert rep.sample_count == 2
    assert rep.accept_count == 2


def test_trust_high_all_accepts(monkeypatch):
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "3")
    rows = [_row("A", True) for _ in range(5)]
    rep = compute_prior_trust("A", rows=rows)
    assert rep.trust_level is PriorTrustLevel.HIGH
    assert rep.accept_rate == 1.0


def test_trust_low_few_accepts(monkeypatch):
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "3")
    rows = [_row("A", True)] + [_row("A", False) for _ in range(9)]
    # 1/10 = 0.10 < 0.25 → LOW
    rep = compute_prior_trust("A", rows=rows)
    assert rep.trust_level is PriorTrustLevel.LOW


def test_trust_medium_band(monkeypatch):
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "3")
    # 1/2 = 0.5 → MEDIUM
    rows = [_row("A", True)] + [_row("A", False)]
    # Need 3 samples for actionable
    rows.append(_row("A", True))  # 2/3 = 0.67 → MEDIUM
    rep = compute_prior_trust("A", rows=rows)
    assert rep.trust_level is PriorTrustLevel.MEDIUM


def test_trust_filters_by_prior_kind(monkeypatch):
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "3")
    rows = (
        [_row("A", True) for _ in range(3)]
        + [_row("B", False) for _ in range(3)]
    )
    rep = compute_prior_trust("A", rows=rows)
    assert rep.sample_count == 3
    assert rep.accept_rate == 1.0


def test_trust_empty_rows():
    rep = compute_prior_trust("A", rows=[])
    assert rep.sample_count == 0
    assert rep.trust_level is PriorTrustLevel.UNKNOWN


# ---------------------------------------------------------------------------
# break_tie — decision matrix
# ---------------------------------------------------------------------------


@dataclass
class _FakeOutcome:
    value: str = "disagreement"


@dataclass
class _FakeVerdict:
    outcome: Any = None


def test_break_tie_master_off_returns_disabled():
    verdict = _FakeVerdict(outcome=_FakeOutcome("disagreement"))
    report = break_tie(verdict, {"r-1": "A"})
    assert report.decision is SchellingDecision.DISABLED
    assert report.master_enabled is False


def test_break_tie_no_tie_when_consensus(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    verdict = _FakeVerdict(outcome=_FakeOutcome("consensus"))
    report = break_tie(verdict, {"r-1": "A"})
    assert report.decision is SchellingDecision.NO_TIE


def test_break_tie_no_tie_when_majority(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    verdict = _FakeVerdict(outcome=_FakeOutcome("majority_consensus"))
    report = break_tie(verdict, {"r-1": "A"})
    assert report.decision is SchellingDecision.NO_TIE


def test_break_tie_no_record_when_disagreement_empty_history(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    verdict = _FakeVerdict(outcome=_FakeOutcome("disagreement"))
    report = break_tie(
        verdict,
        {"r-1": "A", "r-2": "B"},
        rows=[],
    )
    assert report.decision is SchellingDecision.NO_RECORD


def test_break_tie_no_record_under_sample(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "5")
    verdict = _FakeVerdict(outcome=_FakeOutcome("disagreement"))
    # Only 2 records — under sample threshold
    rows = [_row("A", True), _row("A", True)]
    report = break_tie(
        verdict,
        {"r-1": "A", "r-2": "B"},
        rows=rows,
    )
    assert report.decision is SchellingDecision.NO_RECORD


def test_break_tie_chooses_highest_trust(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "3")
    verdict = _FakeVerdict(outcome=_FakeOutcome("disagreement"))
    # A has high trust (3/3); B has low (0/3)
    rows = (
        [_row("A", True) for _ in range(3)]
        + [_row("B", False) for _ in range(3)]
    )
    report = break_tie(
        verdict,
        {"r-A": "A", "r-B": "B"},
        rows=rows,
    )
    assert report.decision is SchellingDecision.TIE_BROKEN
    assert report.chosen_prior_kind == "A"
    assert report.chosen_roll_id == "r-A"


def test_break_tie_consensus_outcome_propagated(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    verdict = _FakeVerdict(outcome=_FakeOutcome("failed"))
    report = break_tie(verdict, {"r-1": "A"}, rows=[])
    assert report.consensus_outcome == "failed"


def test_break_tie_handles_raw_string_verdict(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = break_tie(
        "disagreement",  # raw string instead of object
        {"r-1": "A"},
        rows=[],
    )
    assert report.decision is SchellingDecision.NO_RECORD


def test_break_tie_handles_none_verdict(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = break_tie(None, {"r-1": "A"}, rows=[])
    # None verdict → outcome "" → not "consensus/majority" →
    # falls through to NO_RECORD path
    assert report.decision is SchellingDecision.NO_RECORD


def test_break_tie_trust_table_sorted_desc(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "3")
    verdict = _FakeVerdict(outcome=_FakeOutcome("disagreement"))
    # A=2/3 ≈ 0.67, B=3/3 = 1.0
    rows = (
        [_row("A", True), _row("A", True), _row("A", False)]
        + [_row("B", True) for _ in range(3)]
    )
    report = break_tie(
        verdict,
        {"r-A": "A", "r-B": "B"},
        rows=rows,
    )
    assert report.trust_table[0].prior_kind == "B"
    assert report.trust_table[1].prior_kind == "A"


# ---------------------------------------------------------------------------
# format_tiebreak_panel
# ---------------------------------------------------------------------------


def test_format_panel_master_off():
    out = format_tiebreak_panel()
    assert "disabled" in out
    assert _ENV_MASTER in out


def test_format_panel_master_on_no_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = format_tiebreak_panel()
    assert "no report" in out.lower()


def test_format_panel_tie_broken(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "3")
    verdict = _FakeVerdict(outcome=_FakeOutcome("disagreement"))
    rows = [_row("A", True) for _ in range(3)]
    report = break_tie(verdict, {"r-1": "A"}, rows=rows)
    out = format_tiebreak_panel(report)
    assert "Schelling Tie-Break" in out
    assert "tie_broken" in out


# ---------------------------------------------------------------------------
# to_dict shape
# ---------------------------------------------------------------------------


def test_outcome_record_to_dict():
    rec = PriorOutcomeRecord(
        prior_kind="A",
        op_id="op",
        ast_signature="sig",
        was_accepted=True,
        observed_at_unix=1.0,
    )
    d = rec.to_dict()
    assert d["kind"] == "prior_outcome"
    assert d["schema_version"] == SCHELLING_PRIOR_SCHEMA_VERSION


def test_trust_report_to_dict():
    rep = PriorTrustReport(
        prior_kind="A",
        sample_count=5,
        accept_count=3,
        accept_rate=0.6,
        trust_level=PriorTrustLevel.MEDIUM,
    )
    d = rep.to_dict()
    assert d["trust_level"] == "medium"
    assert d["schema_version"] == SCHELLING_PRIOR_SCHEMA_VERSION


def test_tiebreak_report_to_dict(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "3")
    verdict = _FakeVerdict(outcome=_FakeOutcome("disagreement"))
    rows = [_row("A", True) for _ in range(3)]
    report = break_tie(verdict, {"r-1": "A"}, rows=rows)
    d = report.to_dict()
    assert d["decision"] == "tie_broken"
    assert d["schema_version"] == SCHELLING_PRIOR_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# AST pins — canonical-source pass
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/"
        "schelling_consensus_prior.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 5


@pytest.mark.parametrize(
    "name_part",
    [
        "decision_taxonomy_closed",
        "trust_taxonomy_closed",
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


# ---------------------------------------------------------------------------
# AST pins — synthetic regressions
# ---------------------------------------------------------------------------


def test_pin_decision_synthetic_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "decision_taxonomy_closed" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class SchellingDecision(str, enum.Enum):\n"
        "    NO_TIE = 'no_tie'\n"
        "    TIE_BROKEN = 'tie_broken'\n"
        "    NO_RECORD = 'no_record'\n"
        # missing DISABLED
    )
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


def test_pin_trust_synthetic_extra():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "trust_taxonomy_closed" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class PriorTrustLevel(str, enum.Enum):\n"
        "    UNKNOWN = 'unknown'\n"
        "    LOW = 'low'\n"
        "    MEDIUM = 'medium'\n"
        "    HIGH = 'high'\n"
        "    EXTRA = 'extra'\n"
    )
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


def test_pin_authority_synthetic_dispatch_forbidden():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.governance."
        "verification.multi_prior_dispatch import x\n"
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
    assert count == 4
    names = {spec.name for spec in reg.registered}
    expected = {
        _ENV_MASTER,
        _ENV_PERSIST,
        _ENV_MAX_RECORDS,
        _ENV_MIN_SAMPLE,
    }
    assert expected.issubset(names)


def test_flag_master_default_false_seed():
    reg = _FakeRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered if s.name == _ENV_MASTER
    )
    assert master.default is False


# ---------------------------------------------------------------------------
# Full roundtrip on disk
# ---------------------------------------------------------------------------


def test_full_roundtrip_record_then_tie_break(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MIN_SAMPLE, "3")
    # Persist 3 A-accepts + 3 B-rejects to disk
    for _ in range(3):
        record_prior_outcome("A", "op", "sig", True)
    for _ in range(3):
        record_prior_outcome("B", "op", "sig", False)
    verdict = _FakeVerdict(outcome=_FakeOutcome("disagreement"))
    # No rows= → read from disk
    report = break_tie(verdict, {"r-A": "A", "r-B": "B"})
    assert report.decision is SchellingDecision.TIE_BROKEN
    assert report.chosen_prior_kind == "A"


# ---------------------------------------------------------------------------
# SSE bind
# ---------------------------------------------------------------------------


def test_sse_event_symbol_exists():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert hasattr(ios, "EVENT_TYPE_SCHELLING_TIE_BROKEN")
    assert (
        ios.EVENT_TYPE_SCHELLING_TIE_BROKEN == "schelling_tie_broken"
    )


def test_sse_event_in_valid_set():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert "schelling_tie_broken" in ios._VALID_EVENT_TYPES
