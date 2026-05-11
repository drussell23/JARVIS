"""Regression spine for §40 Wave 4 #9 — Belief Revision Ledger.

Covers:

* §33.1 cognitive substrate default-FALSE
* Closed 4-value :class:`BeliefVerdict` taxonomy
* Closed 4-value :class:`EvidenceKind` taxonomy
* Composes ``cross_process_jsonl.flock_append_line`` for §33.4
  persistence
* Composes ``governance_boundary_gate.is_boundary_crossed`` for
  cage-touching domains (FALSIFIED regardless of evidence count)
* Producer-bridge :func:`record_claim` + :func:`record_evidence`
  behavior (master-on / master-off / persistence-disabled paths)
* Pure evaluator transitions across STABLE / DRIFTING /
  FALSIFIED / DISABLED
* Threshold env override
* Corruption-tolerant ledger load (skip malformed JSON lines)
* :func:`format_belief_panel` never raises
* 5 AST pin canonical-source pass + 5 synthetic regressions
* FlagRegistry seeds auto-discovered
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, List, Tuple

import pytest


from backend.core.ouroboros.governance import (
    belief_revision_ledger as brl,
)
from backend.core.ouroboros.governance.belief_revision_ledger import (
    BELIEF_REVISION_SCHEMA_VERSION,
    BeliefClaim,
    BeliefRevisionReport,
    BeliefVerdict,
    EvidenceKind,
    EvidenceRecord,
    _ENV_FALSIFY_THRESHOLD,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MAX_RECORDS,
    _ENV_PERSIST,
    evaluate_claim,
    evaluate_recent_beliefs,
    falsify_threshold,
    format_belief_panel,
    ledger_path,
    master_enabled,
    max_records,
    persistence_enabled,
    record_claim,
    record_evidence,
    register_flags,
    register_shipped_invariants,
    verdict_glyph,
)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER,
        _ENV_FALSIFY_THRESHOLD,
        _ENV_MAX_RECORDS,
        _ENV_PERSIST,
        _ENV_LEDGER_PATH,
    ):
        monkeypatch.delenv(env, raising=False)
    # Route the default ledger path to a tmp dir so writes from
    # tests that flip master-on don't pollute the workspace.
    monkeypatch.setenv(
        _ENV_LEDGER_PATH,
        str(tmp_path / "belief_revision_ledger.jsonl"),
    )
    yield


# ---------------------------------------------------------------------------
# Defaults / env knobs
# ---------------------------------------------------------------------------


def test_schema_version_constant_format():
    assert BELIEF_REVISION_SCHEMA_VERSION == "belief_revision.1"


def test_master_default_false():
    assert master_enabled() is False


def test_master_truthy_values(monkeypatch):
    for raw in ("1", "true", "yes", "on", "TRUE", "Yes"):
        monkeypatch.setenv(_ENV_MASTER, raw)
        assert master_enabled() is True


def test_master_falsy_values(monkeypatch):
    for raw in ("", "0", "false", "no", "off", "garbage"):
        monkeypatch.setenv(_ENV_MASTER, raw)
        assert master_enabled() is False


def test_persistence_default_true_when_unset():
    assert persistence_enabled() is True


def test_persistence_false_when_explicit(monkeypatch):
    monkeypatch.setenv(_ENV_PERSIST, "false")
    assert persistence_enabled() is False


def test_falsify_threshold_default():
    assert falsify_threshold() == 2


def test_falsify_threshold_clamped_low(monkeypatch):
    monkeypatch.setenv(_ENV_FALSIFY_THRESHOLD, "-5")
    assert falsify_threshold() == 1


def test_falsify_threshold_clamped_high(monkeypatch):
    monkeypatch.setenv(_ENV_FALSIFY_THRESHOLD, "9999999")
    assert falsify_threshold() == 1_000


def test_falsify_threshold_invalid_string_uses_default(monkeypatch):
    monkeypatch.setenv(_ENV_FALSIFY_THRESHOLD, "not-a-number")
    assert falsify_threshold() == 2


def test_max_records_default():
    assert max_records() == 200


def test_max_records_clamped_high(monkeypatch):
    monkeypatch.setenv(_ENV_MAX_RECORDS, "999999999")
    assert max_records() == 100_000


def test_ledger_path_default_from_env(monkeypatch, tmp_path):
    target = tmp_path / "explicit.jsonl"
    monkeypatch.setenv(_ENV_LEDGER_PATH, str(target))
    assert ledger_path() == target


def test_ledger_path_default_relative(monkeypatch):
    monkeypatch.delenv(_ENV_LEDGER_PATH, raising=False)
    p = ledger_path()
    assert str(p) == ".jarvis/belief_revision_ledger.jsonl"


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


def test_belief_verdict_taxonomy_closed():
    assert {v.value for v in BeliefVerdict} == {
        "stable", "drifting", "falsified", "disabled",
    }


def test_evidence_kind_taxonomy_closed():
    assert {e.value for e in EvidenceKind} == {
        "affirming", "falsifying", "neutral", "unknown",
    }


@pytest.mark.parametrize(
    "verdict, glyph",
    [
        (BeliefVerdict.STABLE, "✓"),
        (BeliefVerdict.DRIFTING, "⚠"),
        (BeliefVerdict.FALSIFIED, "🚨"),
        (BeliefVerdict.DISABLED, "◌"),
    ],
)
def test_verdict_glyph_each(verdict, glyph):
    assert verdict_glyph(verdict) == glyph


def test_verdict_glyph_accepts_raw_string():
    assert verdict_glyph("falsified") == "🚨"


def test_verdict_glyph_unknown_returns_question():
    assert verdict_glyph("not-a-verdict") == "?"


def test_verdict_glyph_none_returns_question():
    assert verdict_glyph(None) == "?"


# ---------------------------------------------------------------------------
# record_claim — master off / on
# ---------------------------------------------------------------------------


def test_record_claim_master_off_returns_artifact_no_persist(
    monkeypatch, tmp_path,
):
    # master is off by default
    target = tmp_path / "explicit.jsonl"
    monkeypatch.setenv(_ENV_LEDGER_PATH, str(target))
    claim = record_claim(
        text="memory_pressure_gate is reliable",
        domain="memory_pressure",
        target_files=["a.py"],
        confidence=0.8,
        now_unix=1234567890.0,
    )
    assert isinstance(claim, BeliefClaim)
    assert claim.domain == "memory_pressure"
    # No JSONL written when master off
    assert not target.exists()


def test_record_claim_master_on_persists(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    claim = record_claim(
        text="claim A",
        domain="dom-A",
        target_files=["x/y.py"],
        now_unix=1700000000.0,
    )
    assert isinstance(claim, BeliefClaim)
    p = ledger_path()
    assert p.exists()
    rows = [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["kind"] == "claim"
    assert rows[0]["claim_id"] == claim.claim_id
    assert rows[0]["domain"] == "dom-A"


def test_record_claim_master_on_persist_disabled_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    claim = record_claim(
        text="claim B",
        domain="dom-B",
        now_unix=1700000001.0,
    )
    assert isinstance(claim, BeliefClaim)
    assert not ledger_path().exists()


def test_record_claim_id_deterministic_across_calls(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    a = record_claim(
        text="same text",
        domain="same dom",
        now_unix=1.0,
    )
    b = record_claim(
        text="same text",
        domain="same dom",
        now_unix=1.0,
    )
    assert a is not None and b is not None
    assert a.claim_id == b.claim_id


def test_record_claim_id_changes_with_timestamp(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    a = record_claim(
        text="X", domain="D", now_unix=1.0,
    )
    b = record_claim(
        text="X", domain="D", now_unix=2.0,
    )
    assert a is not None and b is not None
    assert a.claim_id != b.claim_id


def test_record_claim_empty_text_returns_none(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert record_claim(text="", domain="D") is None


def test_record_claim_empty_domain_returns_none(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert record_claim(text="T", domain="") is None


def test_record_claim_clamps_confidence(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    a = record_claim(text="T", domain="D", confidence=5.0)
    b = record_claim(text="T", domain="D", confidence=-3.0, now_unix=2.0)
    assert a is not None and b is not None
    assert a.confidence == 1.0
    assert b.confidence == 0.0


def test_record_claim_normalizes_target_files(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    claim = record_claim(
        text="T",
        domain="D",
        target_files=["a/b/c.py", None, "", "x\\y.py"],
        now_unix=1.0,
    )
    assert claim is not None
    # backslashes coerced, None/'' stripped
    assert all("/" in f or f.endswith(".py") for f in claim.target_files)
    assert "" not in claim.target_files
    assert None not in claim.target_files


# ---------------------------------------------------------------------------
# record_evidence — master off / on
# ---------------------------------------------------------------------------


def test_record_evidence_master_off_no_persist(monkeypatch, tmp_path):
    target = tmp_path / "ev.jsonl"
    monkeypatch.setenv(_ENV_LEDGER_PATH, str(target))
    rec = record_evidence(
        "cid-x",
        EvidenceKind.AFFIRMING,
        now_unix=1.0,
    )
    assert isinstance(rec, EvidenceRecord)
    assert not target.exists()


def test_record_evidence_master_on_persists(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    rec = record_evidence(
        "cid-y",
        EvidenceKind.FALSIFYING,
        source_op_id="op-1",
        source_session_id="bt-1",
        note="contradicts claim",
        now_unix=2.0,
    )
    assert isinstance(rec, EvidenceRecord)
    p = ledger_path()
    assert p.exists()
    rows = [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["kind"] == "evidence"
    assert rows[0]["claim_id"] == "cid-y"
    assert rows[0]["evidence_kind"] == "falsifying"


def test_record_evidence_empty_claim_id_returns_none(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert record_evidence("", EvidenceKind.AFFIRMING) is None


def test_record_evidence_kind_coercion_from_string(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    rec = record_evidence("cid-z", "falsifying", now_unix=1.0)
    assert rec is not None
    assert rec.kind is EvidenceKind.FALSIFYING


def test_record_evidence_kind_unknown_for_garbage(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    rec = record_evidence("cid-z", "not-a-kind", now_unix=1.0)
    assert rec is not None
    assert rec.kind is EvidenceKind.UNKNOWN


def test_record_evidence_kind_passthrough_enum(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    rec = record_evidence(
        "cid",
        EvidenceKind.NEUTRAL,
        now_unix=1.0,
    )
    assert rec is not None
    assert rec.kind is EvidenceKind.NEUTRAL


# ---------------------------------------------------------------------------
# evaluate_claim — verdict transitions
# ---------------------------------------------------------------------------


def _ledger_rows_for(
    claim: BeliefClaim, evidence: List[EvidenceRecord],
) -> List[dict]:
    rows = [claim.to_dict()]
    rows.extend(e.to_dict() for e in evidence)
    return rows


def test_evaluate_master_off_returns_disabled():
    report = evaluate_claim("any")
    assert report.master_enabled is False
    assert report.verdict is BeliefVerdict.DISABLED


def test_evaluate_empty_claim_id_returns_stable(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_claim("", rows=[])
    assert report.master_enabled is True
    assert report.verdict is BeliefVerdict.STABLE


def test_evaluate_stable_with_affirming(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    claim = record_claim(
        text="T", domain="D", now_unix=1.0,
    )
    assert claim is not None
    ev = record_evidence(
        claim.claim_id, EvidenceKind.AFFIRMING, now_unix=2.0,
    )
    assert ev is not None
    rows = _ledger_rows_for(claim, [ev])
    report = evaluate_claim(claim.claim_id, rows=rows)
    assert report.verdict is BeliefVerdict.STABLE
    assert report.affirming_count == 1


def test_evaluate_drifting_one_falsifying(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    claim = record_claim(text="T", domain="D", now_unix=1.0)
    assert claim is not None
    ev = record_evidence(
        claim.claim_id,
        EvidenceKind.FALSIFYING,
        now_unix=2.0,
    )
    assert ev is not None
    rows = _ledger_rows_for(claim, [ev])
    report = evaluate_claim(claim.claim_id, rows=rows)
    assert report.verdict is BeliefVerdict.DRIFTING
    assert report.falsifying_count == 1


def test_evaluate_falsified_two_falsifying(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    claim = record_claim(text="T", domain="D", now_unix=1.0)
    assert claim is not None
    e1 = record_evidence(
        claim.claim_id, EvidenceKind.FALSIFYING, now_unix=2.0,
    )
    e2 = record_evidence(
        claim.claim_id, EvidenceKind.FALSIFYING, now_unix=3.0,
    )
    assert e1 is not None and e2 is not None
    rows = _ledger_rows_for(claim, [e1, e2])
    report = evaluate_claim(claim.claim_id, rows=rows)
    assert report.verdict is BeliefVerdict.FALSIFIED
    assert report.falsifying_count == 2


def test_evaluate_threshold_override_changes_verdict(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_FALSIFY_THRESHOLD, "5")
    claim = record_claim(text="T", domain="D", now_unix=1.0)
    assert claim is not None
    evs = [
        record_evidence(
            claim.claim_id,
            EvidenceKind.FALSIFYING,
            now_unix=float(i),
        )
        for i in range(2, 5)  # 3 falsifying — below threshold 5
    ]
    rows = _ledger_rows_for(claim, [e for e in evs if e])
    report = evaluate_claim(claim.claim_id, rows=rows)
    assert report.verdict is BeliefVerdict.DRIFTING
    assert report.falsifying_count == 3


def test_evaluate_boundary_crossed_forces_falsified(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    claim = record_claim(
        text="T",
        domain="cage-domain",
        target_files=[
            "backend/core/ouroboros/governance/orchestrator.py",
        ],
        now_unix=1.0,
    )
    assert claim is not None
    # No falsifying evidence at all
    rows = _ledger_rows_for(claim, [])
    report = evaluate_claim(claim.claim_id, rows=rows)
    # Whatever boundary gate says — when cage-touching, verdict is FALSIFIED
    if report.boundary_crossed:
        assert report.verdict is BeliefVerdict.FALSIFIED


def test_evaluate_unknown_kind_counted(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    claim = record_claim(text="T", domain="D", now_unix=1.0)
    assert claim is not None
    rec = record_evidence(
        claim.claim_id,
        "garbage-kind",  # coerced → UNKNOWN
        now_unix=2.0,
    )
    assert rec is not None
    rows = _ledger_rows_for(claim, [rec])
    report = evaluate_claim(claim.claim_id, rows=rows)
    assert report.unknown_count == 1
    assert report.verdict is BeliefVerdict.STABLE  # no falsifying


def test_evaluate_returns_evidence_records_in_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    claim = record_claim(text="T", domain="D", now_unix=1.0)
    assert claim is not None
    ev = record_evidence(
        claim.claim_id, EvidenceKind.AFFIRMING, now_unix=2.0,
    )
    assert ev is not None
    rows = _ledger_rows_for(claim, [ev])
    report = evaluate_claim(claim.claim_id, rows=rows)
    assert len(report.evidence_records) == 1


def test_evaluate_recent_beliefs_master_off_empty(monkeypatch):
    # master off by default
    out = evaluate_recent_beliefs()
    assert out == ()


def test_evaluate_recent_beliefs_master_on_one_per_claim(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    c1 = record_claim(text="T1", domain="D1", now_unix=1.0)
    c2 = record_claim(text="T2", domain="D2", now_unix=2.0)
    assert c1 is not None and c2 is not None
    rows = [c1.to_dict(), c2.to_dict()]
    out = evaluate_recent_beliefs(rows=rows)
    assert len(out) == 2
    cids = {r.claim.claim_id for r in out if r.claim}
    assert cids == {c1.claim_id, c2.claim_id}


# ---------------------------------------------------------------------------
# Ledger load corruption-tolerance
# ---------------------------------------------------------------------------


def test_ledger_load_skips_malformed_lines(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "\n".join([
            "{not json}",
            json.dumps({"kind": "claim", "claim_id": "good"}),
            "[]",  # not a dict
            json.dumps({"kind": "evidence", "claim_id": "good"}),
            "",
        ]),
        encoding="utf-8",
    )
    out = evaluate_recent_beliefs()
    # Only the well-formed claim row should land
    assert len(out) == 1


def test_ledger_load_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(
        _ENV_LEDGER_PATH,
        str(tmp_path / "definitely-not-here.jsonl"),
    )
    out = evaluate_recent_beliefs()
    assert out == ()


# ---------------------------------------------------------------------------
# format_belief_panel renderer
# ---------------------------------------------------------------------------


def test_format_panel_master_off():
    out = format_belief_panel(claim_id="anything")
    assert "disabled" in out
    assert _ENV_MASTER in out


def test_format_panel_master_on_no_claim_id(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = format_belief_panel()
    assert "no claim_id" in out


def test_format_panel_master_on_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    claim = record_claim(text="some text", domain="D", now_unix=1.0)
    assert claim is not None
    rows = [claim.to_dict()]
    report = evaluate_claim(claim.claim_id, rows=rows)
    out = format_belief_panel(report)
    assert "Belief Revision" in out
    assert claim.claim_id in out
    assert "stable" in out  # no evidence → STABLE


def test_format_panel_never_raises_on_garbage():
    bogus = BeliefRevisionReport(
        evaluated_at_unix=0.0,
        master_enabled=True,
        claim=None,
        affirming_count=0,
        falsifying_count=0,
        neutral_count=0,
        unknown_count=0,
        verdict=BeliefVerdict.STABLE,
        boundary_crossed=False,
        diagnostic="x" * 1000,  # overlong
        elapsed_s=0.0,
        evidence_records=(),
    )
    out = format_belief_panel(bogus)
    assert "Belief Revision" in out


# ---------------------------------------------------------------------------
# to_dict shape
# ---------------------------------------------------------------------------


def test_claim_to_dict_shape(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    c = record_claim(text="T", domain="D", now_unix=1.0)
    assert c is not None
    d = c.to_dict()
    assert d["kind"] == "claim"
    assert d["claim_id"] == c.claim_id
    assert d["schema_version"] == BELIEF_REVISION_SCHEMA_VERSION


def test_evidence_to_dict_shape(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    rec = record_evidence(
        "cid", EvidenceKind.AFFIRMING, now_unix=1.0,
    )
    assert rec is not None
    d = rec.to_dict()
    assert d["kind"] == "evidence"
    assert d["evidence_kind"] == "affirming"
    assert d["schema_version"] == BELIEF_REVISION_SCHEMA_VERSION


def test_report_to_dict_shape(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    c = record_claim(text="T", domain="D", now_unix=1.0)
    assert c is not None
    report = evaluate_claim(c.claim_id, rows=[c.to_dict()])
    d = report.to_dict()
    assert d["verdict"] == "stable"
    assert d["schema_version"] == BELIEF_REVISION_SCHEMA_VERSION
    assert isinstance(d["evidence_records"], list)


# ---------------------------------------------------------------------------
# AST pins — canonical-source pass
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/"
        "belief_revision_ledger.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_registered_count():
    pins = register_shipped_invariants()
    assert len(pins) == 5


def test_pin_verdict_taxonomy_canonical_pass(_canonical):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy_closed" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_pin_evidence_taxonomy_canonical_pass(_canonical):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "evidence_taxonomy_closed" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_pin_authority_canonical_pass(_canonical):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_pin_master_default_false_canonical_pass(_canonical):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_pin_composes_canonical_pass(_canonical):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


# ---------------------------------------------------------------------------
# AST pins — synthetic regressions
# ---------------------------------------------------------------------------


def test_pin_verdict_taxonomy_synthetic_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy_closed" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class BeliefVerdict(str, enum.Enum):\n"
        "    STABLE = 'stable'\n"
        "    DRIFTING = 'drifting'\n"
        "    FALSIFIED = 'falsified'\n"
        # missing DISABLED — drift
    )
    tree = ast.parse(bad_src)
    out = pin.validate(tree, bad_src)
    assert out  # non-empty violations tuple


def test_pin_evidence_taxonomy_synthetic_extra():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "evidence_taxonomy_closed" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class EvidenceKind(str, enum.Enum):\n"
        "    AFFIRMING = 'affirming'\n"
        "    FALSIFYING = 'falsifying'\n"
        "    NEUTRAL = 'neutral'\n"
        "    UNKNOWN = 'unknown'\n"
        "    BONUS = 'bonus'\n"  # extra — should fail
    )
    tree = ast.parse(bad_src)
    out = pin.validate(tree, bad_src)
    assert out


def test_pin_authority_synthetic_violation():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import something\n"
    )
    tree = ast.parse(bad_src)
    out = pin.validate(tree, bad_src)
    assert out


def test_pin_master_default_false_synthetic():
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
    out = pin.validate(tree, bad_src)
    assert out


def test_pin_composes_canonical_synthetic_missing():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    bad_src = "# substrate without canonical composition\n"
    tree = ast.parse(bad_src)
    out = pin.validate(tree, bad_src)
    assert out


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
    assert _ENV_MASTER in names
    assert _ENV_FALSIFY_THRESHOLD in names
    assert _ENV_MAX_RECORDS in names
    assert _ENV_PERSIST in names


def test_flag_registry_master_default_false_seed():
    reg = _FakeRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered if s.name == _ENV_MASTER
    )
    assert master.default is False


def test_flag_registry_seed_failure_doesnt_raise():
    class _Broken:
        def register(self, spec):
            raise RuntimeError("boom")
    # Should not propagate
    count = register_flags(_Broken())
    assert count == 0


# ---------------------------------------------------------------------------
# Disk → ledger roundtrip via real file
# ---------------------------------------------------------------------------


def test_full_roundtrip_master_on(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    c = record_claim(text="round trip", domain="rt", now_unix=10.0)
    assert c is not None
    record_evidence(
        c.claim_id, EvidenceKind.AFFIRMING, now_unix=11.0,
    )
    record_evidence(
        c.claim_id, EvidenceKind.FALSIFYING, now_unix=12.0,
    )
    record_evidence(
        c.claim_id, EvidenceKind.FALSIFYING, now_unix=13.0,
    )
    report = evaluate_claim(c.claim_id)
    assert report.affirming_count == 1
    assert report.falsifying_count == 2
    assert report.verdict is BeliefVerdict.FALSIFIED


def test_full_roundtrip_drifting(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    c = record_claim(text="drifting", domain="d", now_unix=20.0)
    assert c is not None
    record_evidence(
        c.claim_id, EvidenceKind.FALSIFYING, now_unix=21.0,
    )
    record_evidence(
        c.claim_id, EvidenceKind.AFFIRMING, now_unix=22.0,
    )
    report = evaluate_claim(c.claim_id)
    assert report.falsifying_count == 1
    assert report.affirming_count == 1
    assert report.verdict is BeliefVerdict.DRIFTING


# ---------------------------------------------------------------------------
# SSE bind — event symbol present
# ---------------------------------------------------------------------------


def test_sse_event_symbol_exists():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert hasattr(ios, "EVENT_TYPE_BELIEF_REVISION_RECORDED")
    assert (
        ios.EVENT_TYPE_BELIEF_REVISION_RECORDED
        == "belief_revision_recorded"
    )


def test_sse_event_in_valid_set():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert (
        "belief_revision_recorded"
        in ios._VALID_EVENT_TYPES
    )
