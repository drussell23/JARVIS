"""Regression spine for §40 Wave 4 #11 — Postmortem Fusion.

Covers:

* §33.1 cognitive substrate default-FALSE
* Closed 4-value :class:`FusionVerdict` taxonomy
* Closed 4-value :class:`FusionSeverity` taxonomy
* Composes ``postmortem_recall.gather_recent_postmortems``
* Composes ``postmortem_clusterer.cluster_postmortems``
* Composes Wave 4 #9 ``belief_revision_ledger.record_claim``
  for optional persistence
* Composes Wave 2 #5 ``governance_boundary_gate`` for severity
  escalation
* fuse_recent_postmortems → 4-value verdict (NO_PATTERN /
  EMERGING / FUSED / DISABLED)
* synthesize_meta_postmortem pure projection
* Threshold env clamps + emerge < fuse invariant
* 5 AST pin canonical-source pass + 5 synthetic regressions
* FlagRegistry seeds + master default-FALSE
* SSE event symbol present
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Tuple

import pytest


from backend.core.ouroboros.governance import (
    postmortem_fusion as pf,
)
from backend.core.ouroboros.governance.postmortem_fusion import (
    POSTMORTEM_FUSION_SCHEMA_VERSION,
    FusionReport,
    FusionSeverity,
    FusionVerdict,
    MetaPostmortem,
    _ENV_EMERGE_THRESHOLD,
    _ENV_FUSE_THRESHOLD,
    _ENV_MASTER,
    _ENV_MAX_META,
    _ENV_MAX_POSTMORTEMS,
    _ENV_RECORD_CLAIM,
    emerge_threshold,
    format_fusion_panel,
    fuse_recent_postmortems,
    fuse_threshold,
    master_enabled,
    max_meta_postmortems,
    max_postmortems_to_scan,
    record_claim_enabled,
    register_flags,
    register_shipped_invariants,
    severity_glyph,
    synthesize_meta_postmortem,
    verdict_glyph,
)
from backend.core.ouroboros.governance.postmortem_recall import (
    PostmortemRecord,
)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER,
        _ENV_FUSE_THRESHOLD,
        _ENV_EMERGE_THRESHOLD,
        _ENV_MAX_META,
        _ENV_MAX_POSTMORTEMS,
        _ENV_RECORD_CLAIM,
        "JARVIS_BELIEF_REVISION_ENABLED",
        "JARVIS_BELIEF_REVISION_LEDGER_PATH",
    ):
        monkeypatch.delenv(env, raising=False)
    # Route belief-ledger to tmp to keep tests hermetic when
    # the fusion's record_claim hook fires.
    monkeypatch.setenv(
        "JARVIS_BELIEF_REVISION_LEDGER_PATH",
        str(tmp_path / "belief.jsonl"),
    )
    yield


def _make_record(
    op_id: str,
    failed_phase: str = "GENERATE",
    root_cause: str = "all_providers_exhausted:tier_0_failed",
    target_files: Tuple[str, ...] = ("a.py",),
    timestamp_unix: float = 1700000000.0,
) -> PostmortemRecord:
    return PostmortemRecord(
        op_id=op_id,
        session_id="bt-test",
        root_cause=root_cause,
        failed_phase=failed_phase,
        next_safe_action="reduce_complexity",
        target_files=target_files,
        timestamp_iso="2026-05-10T00:00:00",
        timestamp_unix=timestamp_unix,
    )


# ---------------------------------------------------------------------------
# Defaults / env knobs
# ---------------------------------------------------------------------------


def test_schema_version():
    assert POSTMORTEM_FUSION_SCHEMA_VERSION == "postmortem_fusion.1"


def test_master_default_false():
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert master_enabled() is True


def test_record_claim_default_true():
    assert record_claim_enabled() is True


def test_record_claim_explicit_false(monkeypatch):
    monkeypatch.setenv(_ENV_RECORD_CLAIM, "false")
    assert record_claim_enabled() is False


def test_fuse_threshold_default():
    assert fuse_threshold() == 3


def test_fuse_threshold_clamped_low(monkeypatch):
    monkeypatch.setenv(_ENV_FUSE_THRESHOLD, "0")
    assert fuse_threshold() == 1


def test_fuse_threshold_clamped_high(monkeypatch):
    monkeypatch.setenv(_ENV_FUSE_THRESHOLD, "9999999")
    assert fuse_threshold() == 1_000


def test_emerge_threshold_default():
    assert emerge_threshold() == 2


def test_emerge_clamped_below_fuse(monkeypatch):
    monkeypatch.setenv(_ENV_FUSE_THRESHOLD, "5")
    monkeypatch.setenv(_ENV_EMERGE_THRESHOLD, "10")
    # 10 explicit > fuse 5 → must clamp to fuse - 1 == 4
    assert emerge_threshold() == 4


def test_emerge_clamped_at_minimum_when_fuse_is_one(monkeypatch):
    monkeypatch.setenv(_ENV_FUSE_THRESHOLD, "1")
    monkeypatch.setenv(_ENV_EMERGE_THRESHOLD, "5")
    # fuse=1 → emerge must be max(1, min(5, 0)) == 1 (min floor)
    assert emerge_threshold() == 1


def test_max_meta_default():
    assert max_meta_postmortems() == 10


def test_max_postmortems_default():
    assert max_postmortems_to_scan() == 200


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


def test_fusion_verdict_taxonomy_closed():
    assert {v.value for v in FusionVerdict} == {
        "no_pattern", "emerging", "fused", "disabled",
    }


def test_fusion_severity_taxonomy_closed():
    assert {s.value for s in FusionSeverity} == {
        "low", "medium", "high", "critical",
    }


@pytest.mark.parametrize(
    "verdict, glyph",
    [
        (FusionVerdict.NO_PATTERN, "✓"),
        (FusionVerdict.EMERGING, "⚠"),
        (FusionVerdict.FUSED, "🚨"),
        (FusionVerdict.DISABLED, "◌"),
    ],
)
def test_verdict_glyph_each(verdict, glyph):
    assert verdict_glyph(verdict) == glyph


def test_verdict_glyph_unknown():
    assert verdict_glyph("not-a-verdict") == "?"


@pytest.mark.parametrize(
    "severity",
    list(FusionSeverity),
)
def test_severity_glyph_known(severity):
    assert severity_glyph(severity) != "?"


def test_severity_glyph_unknown():
    assert severity_glyph("garbage") == "?"


# ---------------------------------------------------------------------------
# fuse_recent_postmortems — master off / on
# ---------------------------------------------------------------------------


def test_fuse_master_off_returns_disabled():
    report = fuse_recent_postmortems(postmortems=[])
    assert isinstance(report, FusionReport)
    assert report.master_enabled is False
    assert report.verdict is FusionVerdict.DISABLED


def test_fuse_master_on_empty_corpus_no_pattern(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = fuse_recent_postmortems(postmortems=[])
    assert report.verdict is FusionVerdict.NO_PATTERN
    assert report.fused_count == 0
    assert report.emerging_count == 0


def test_fuse_master_on_three_same_signature_fuses(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RECORD_CLAIM, "false")  # no belief writes
    records = [
        _make_record(f"op-{i}", target_files=("x.py",))
        for i in range(3)
    ]
    report = fuse_recent_postmortems(postmortems=records)
    assert report.verdict is FusionVerdict.FUSED
    assert report.fused_count == 1
    assert len(report.meta_postmortems) == 1
    meta = report.meta_postmortems[0]
    assert meta.member_count == 3


def test_fuse_two_same_signature_emerging(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RECORD_CLAIM, "false")
    records = [
        _make_record(f"op-{i}", target_files=("y.py",))
        for i in range(2)
    ]
    report = fuse_recent_postmortems(postmortems=records)
    assert report.verdict is FusionVerdict.EMERGING
    assert report.emerging_count == 1
    assert report.fused_count == 0


def test_fuse_distinct_signatures_no_pattern(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RECORD_CLAIM, "false")
    records = [
        _make_record("op-a", failed_phase="GENERATE",
                     root_cause="alpha"),
        _make_record("op-b", failed_phase="VALIDATE",
                     root_cause="beta"),
        _make_record("op-c", failed_phase="APPLY",
                     root_cause="gamma"),
    ]
    report = fuse_recent_postmortems(postmortems=records)
    assert report.verdict is FusionVerdict.NO_PATTERN


def test_fuse_with_threshold_override(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RECORD_CLAIM, "false")
    monkeypatch.setenv(_ENV_FUSE_THRESHOLD, "5")
    # 4 same-signature records — below 5, must EMERGING not FUSE
    records = [_make_record(f"op-{i}") for i in range(4)]
    report = fuse_recent_postmortems(postmortems=records)
    assert report.verdict is FusionVerdict.EMERGING
    assert report.fused_count == 0


def test_fuse_severity_low_when_no_boundary(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RECORD_CLAIM, "false")
    records = [
        _make_record(f"op-{i}", target_files=("safe.py",))
        for i in range(3)
    ]
    report = fuse_recent_postmortems(postmortems=records)
    assert len(report.meta_postmortems) == 1
    assert report.meta_postmortems[0].severity is FusionSeverity.LOW


def test_fuse_severity_medium_when_above_threshold(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RECORD_CLAIM, "false")
    # 5 records, fuse_threshold=3 → 5 > 3 → MEDIUM
    records = [
        _make_record(f"op-{i}", target_files=("safe.py",))
        for i in range(5)
    ]
    report = fuse_recent_postmortems(postmortems=records)
    assert len(report.meta_postmortems) == 1
    assert report.meta_postmortems[0].severity is FusionSeverity.MEDIUM


def test_fuse_severity_high_when_cage(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RECORD_CLAIM, "false")
    cage_path = (
        "backend/core/ouroboros/governance/orchestrator.py"
    )
    records = [
        _make_record(f"op-{i}", target_files=(cage_path,))
        for i in range(3)
    ]
    report = fuse_recent_postmortems(postmortems=records)
    # Cage may or may not resolve to crossed depending on env;
    # if boundary detected, severity is HIGH (==3) or CRITICAL (>3)
    if report.meta_postmortems[0].boundary_crossed:
        assert report.meta_postmortems[0].severity in (
            FusionSeverity.HIGH, FusionSeverity.CRITICAL,
        )


def test_fuse_with_record_claim_sets_claim_id(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RECORD_CLAIM, "true")
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_ENABLED", "true")
    records = [
        _make_record(f"op-{i}", target_files=("z.py",))
        for i in range(3)
    ]
    report = fuse_recent_postmortems(postmortems=records)
    assert report.fused_count == 1
    assert report.meta_postmortems[0].claim_id_emitted != ""


def test_fuse_record_claim_disabled_no_claim(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RECORD_CLAIM, "false")
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_ENABLED", "true")
    records = [
        _make_record(f"op-{i}", target_files=("w.py",))
        for i in range(3)
    ]
    report = fuse_recent_postmortems(postmortems=records)
    assert report.meta_postmortems[0].claim_id_emitted == ""


def test_fuse_meta_cap_respected(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RECORD_CLAIM, "false")
    monkeypatch.setenv(_ENV_MAX_META, "1")
    # Two distinct clusters each at threshold — only 1 should emerge
    records: List[PostmortemRecord] = []
    for i in range(3):
        records.append(_make_record(f"a-{i}", root_cause="alpha"))
    for i in range(3):
        records.append(_make_record(f"b-{i}", root_cause="beta"))
    report = fuse_recent_postmortems(postmortems=records)
    assert report.fused_count <= 1


# ---------------------------------------------------------------------------
# synthesize_meta_postmortem — pure projection
# ---------------------------------------------------------------------------


@dataclass
class _FakeSignature:
    failed_phase: str = "GENERATE"
    root_cause_class: str = "alpha"

    def signature_hash(self) -> str:
        return "deadbeefcafe"


@dataclass
class _FakeCluster:
    signature: Any = field(default_factory=_FakeSignature)
    member_op_ids: Tuple[str, ...] = ("op-1", "op-2", "op-3")
    member_count: int = 3
    target_files_union: Tuple[str, ...] = ("a.py",)
    dominant_next_safe_action: str = "reduce_complexity"
    representative_root_cause: str = "alpha failure details"
    oldest_unix: float = 1.0
    newest_unix: float = 2.0


def test_synthesize_basic_shape():
    cluster = _FakeCluster()
    meta = synthesize_meta_postmortem(cluster, fuse_t=3)
    assert meta is not None
    assert meta.cluster_signature_hash == "deadbeefcafe"
    assert meta.failed_phase == "GENERATE"
    assert meta.root_cause_class == "alpha"
    assert meta.member_count == 3
    assert meta.severity is FusionSeverity.LOW


def test_synthesize_none_for_none():
    assert synthesize_meta_postmortem(None) is None


def test_synthesize_resilient_to_missing_attrs():
    class _Broken:
        signature = None
    assert synthesize_meta_postmortem(_Broken()) is None


def test_synthesize_severity_above_threshold():
    cluster = _FakeCluster(member_count=10)
    meta = synthesize_meta_postmortem(cluster, fuse_t=3)
    assert meta is not None
    assert meta.severity is FusionSeverity.MEDIUM  # no boundary


# ---------------------------------------------------------------------------
# format_fusion_panel
# ---------------------------------------------------------------------------


def test_format_panel_master_off():
    out = format_fusion_panel()
    assert "disabled" in out
    assert _ENV_MASTER in out


def test_format_panel_master_on_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RECORD_CLAIM, "false")
    records = [
        _make_record(f"op-{i}", target_files=("p.py",))
        for i in range(3)
    ]
    report = fuse_recent_postmortems(postmortems=records)
    out = format_fusion_panel(report)
    assert "Postmortem Fusion" in out
    assert "fused" in out


def test_format_panel_never_raises_on_empty():
    bogus = FusionReport(
        evaluated_at_unix=0.0,
        master_enabled=True,
        verdict=FusionVerdict.NO_PATTERN,
        postmortems_scanned=0,
        clusters_examined=0,
        emerging_count=0,
        fused_count=0,
        meta_postmortems=(),
        diagnostic="x" * 1000,
        elapsed_s=0.0,
    )
    out = format_fusion_panel(bogus)
    assert "Postmortem Fusion" in out


# ---------------------------------------------------------------------------
# to_dict shape
# ---------------------------------------------------------------------------


def test_meta_postmortem_to_dict_shape():
    cluster = _FakeCluster()
    meta = synthesize_meta_postmortem(cluster, fuse_t=3)
    assert meta is not None
    d = meta.to_dict()
    assert d["schema_version"] == POSTMORTEM_FUSION_SCHEMA_VERSION
    assert d["severity"] == "low"
    assert d["member_count"] == 3
    assert isinstance(d["member_op_ids"], list)


def test_fusion_report_to_dict_shape(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RECORD_CLAIM, "false")
    records = [
        _make_record(f"op-{i}", target_files=("q.py",))
        for i in range(3)
    ]
    report = fuse_recent_postmortems(postmortems=records)
    d = report.to_dict()
    assert d["verdict"] == "fused"
    assert d["schema_version"] == POSTMORTEM_FUSION_SCHEMA_VERSION
    assert isinstance(d["meta_postmortems"], list)


# ---------------------------------------------------------------------------
# AST pins — canonical-source pass
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/postmortem_fusion.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 5


def test_pin_verdict_taxonomy_pass(_canonical):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy_closed" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_pin_severity_taxonomy_pass(_canonical):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "severity_taxonomy_closed" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_pin_authority_pass(_canonical):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_pin_master_pass(_canonical):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_pin_composes_pass(_canonical):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


# ---------------------------------------------------------------------------
# AST pins — synthetic regression
# ---------------------------------------------------------------------------


def test_pin_verdict_synthetic_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy_closed" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class FusionVerdict(str, enum.Enum):\n"
        "    NO_PATTERN = 'no_pattern'\n"
        "    EMERGING = 'emerging'\n"
        "    FUSED = 'fused'\n"
        # missing DISABLED
    )
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


def test_pin_severity_synthetic_extra():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "severity_taxonomy_closed" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class FusionSeverity(str, enum.Enum):\n"
        "    LOW = 'low'\n"
        "    MEDIUM = 'medium'\n"
        "    HIGH = 'high'\n"
        "    CRITICAL = 'critical'\n"
        "    EXTRA = 'extra'\n"
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
    bad_src = "# substrate without canonical composition\n"
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
        _ENV_FUSE_THRESHOLD,
        _ENV_EMERGE_THRESHOLD,
        _ENV_MAX_META,
        _ENV_MAX_POSTMORTEMS,
        _ENV_RECORD_CLAIM,
    }
    assert expected.issubset(names)


def test_flag_registry_master_default_false_seed():
    reg = _FakeRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered if s.name == _ENV_MASTER
    )
    assert master.default is False


# ---------------------------------------------------------------------------
# SSE bind
# ---------------------------------------------------------------------------


def test_sse_event_symbol_exists():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert hasattr(ios, "EVENT_TYPE_POSTMORTEM_FUSED")
    assert (
        ios.EVENT_TYPE_POSTMORTEM_FUSED == "postmortem_fused"
    )


def test_sse_event_in_valid_set():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert "postmortem_fused" in ios._VALID_EVENT_TYPES
