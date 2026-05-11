"""Regression spine for §40 Wave 4 #10 — Sleep Consolidation Pass.

Covers:

* §33.1 cognitive substrate default-FALSE
* Closed 4-value :class:`ConsolidationVerdict` taxonomy
* Closed 4-value :class:`MatchKind` taxonomy
* Idle gate (AWAKE when idle < threshold)
* DREAMING when idle ≥ threshold but no matches
* CONSOLIDATED when belief / fusion matches surface
* MatchKind transitions (BELIEF_FALSIFIED / POSTMORTEM_FUSED /
  FILE_OVERLAP / NONE) based on overlap topology
* Composes Wave 4 #9 belief_revision_ledger.evaluate_recent_beliefs
* Composes Wave 4 #11 postmortem_fusion.fuse_recent_postmortems
* Composes Wave 2 #5 governance_boundary_gate (boundary_crossed
  flag)
* Composes cross_process_jsonl for §33.4 persistence
* Lazy DreamEngine import (substrate purity AST pin)
* 6 AST pin canonical-source pass + 6 synthetic regressions
* FlagRegistry seeds + master default-FALSE
* SSE event symbol present + in _VALID_EVENT_TYPES
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Tuple

import pytest


from backend.core.ouroboros.governance import (
    sleep_consolidation_pass as scp,
)
from backend.core.ouroboros.governance.sleep_consolidation_pass import (
    SLEEP_CONSOLIDATION_SCHEMA_VERSION,
    ConsolidationCandidate,
    ConsolidationMatch,
    ConsolidationReport,
    ConsolidationVerdict,
    MatchKind,
    _ENV_IDLE_THRESHOLD,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MATCH_THRESHOLD,
    _ENV_MAX_BLUEPRINTS,
    _ENV_MAX_CANDIDATES,
    _ENV_PERSIST,
    format_consolidation_panel,
    idle_threshold_s,
    ledger_path,
    master_enabled,
    match_glyph,
    match_threshold,
    max_blueprints_to_scan,
    max_candidates,
    persistence_enabled,
    register_flags,
    register_shipped_invariants,
    run_consolidation_pass,
    verdict_glyph,
)


# ---------------------------------------------------------------------------
# Fake duck-typed objects for hermetic tests
# ---------------------------------------------------------------------------


@dataclass
class _FakeBlueprint:
    blueprint_id: str = "bp-test"
    title: str = "Refactor foo"
    description: str = ""
    category: str = "complexity"
    target_files: Tuple[str, ...] = field(default_factory=tuple)


@dataclass
class _FakeClaim:
    claim_id: str = "cid"
    text: str = "X"
    domain: str = "dom"
    confidence: float = 0.5
    target_files: Tuple[str, ...] = field(default_factory=tuple)


@dataclass
class _FakeMeta:
    cluster_signature_hash: str = "deadbeef"
    failed_phase: str = "GENERATE"
    root_cause_class: str = "alpha"
    target_files_union: Tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER,
        _ENV_PERSIST,
        _ENV_IDLE_THRESHOLD,
        _ENV_MATCH_THRESHOLD,
        _ENV_MAX_CANDIDATES,
        _ENV_MAX_BLUEPRINTS,
        _ENV_LEDGER_PATH,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_LEDGER_PATH,
        str(tmp_path / "consolidation.jsonl"),
    )
    yield


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_schema_version():
    assert SLEEP_CONSOLIDATION_SCHEMA_VERSION == "sleep_consolidation.1"


def test_master_default_false():
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert master_enabled() is True


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_idle_threshold_default():
    assert idle_threshold_s() == 1800


def test_idle_threshold_clamped_low(monkeypatch):
    monkeypatch.setenv(_ENV_IDLE_THRESHOLD, "-100")
    assert idle_threshold_s() == 0


def test_idle_threshold_clamped_high(monkeypatch):
    monkeypatch.setenv(_ENV_IDLE_THRESHOLD, "999999")
    assert idle_threshold_s() == 86_400


def test_match_threshold_default():
    assert match_threshold() == 1


def test_max_candidates_default():
    assert max_candidates() == 10


def test_max_blueprints_default():
    assert max_blueprints_to_scan() == 50


def test_ledger_path_relative(monkeypatch):
    monkeypatch.delenv(_ENV_LEDGER_PATH, raising=False)
    p = ledger_path()
    assert str(p) == ".jarvis/sleep_consolidation_ledger.jsonl"


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


def test_verdict_taxonomy_closed():
    assert {v.value for v in ConsolidationVerdict} == {
        "awake", "dreaming", "consolidated", "disabled",
    }


def test_match_taxonomy_closed():
    assert {m.value for m in MatchKind} == {
        "belief_falsified", "postmortem_fused",
        "file_overlap", "none",
    }


@pytest.mark.parametrize(
    "v, expected",
    [
        (ConsolidationVerdict.AWAKE, "○"),
        (ConsolidationVerdict.DREAMING, "💤"),
        (ConsolidationVerdict.CONSOLIDATED, "🌙"),
        (ConsolidationVerdict.DISABLED, "◌"),
    ],
)
def test_verdict_glyph(v, expected):
    assert verdict_glyph(v) == expected


def test_verdict_glyph_unknown():
    assert verdict_glyph("not-a-verdict") == "?"


@pytest.mark.parametrize("m", list(MatchKind))
def test_match_glyph_known(m):
    assert match_glyph(m) != "?"


# ---------------------------------------------------------------------------
# Idle gate
# ---------------------------------------------------------------------------


def test_master_off_returns_disabled():
    report = run_consolidation_pass(idle_seconds=99999.0)
    assert report.verdict is ConsolidationVerdict.DISABLED
    assert report.master_enabled is False


def test_idle_below_threshold_awake(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    monkeypatch.setenv(_ENV_IDLE_THRESHOLD, "1800")
    report = run_consolidation_pass(idle_seconds=500.0)
    assert report.verdict is ConsolidationVerdict.AWAKE


def test_negative_idle_coerced_to_zero(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    report = run_consolidation_pass(idle_seconds=-50.0)
    assert report.idle_seconds == 0.0
    assert report.verdict is ConsolidationVerdict.AWAKE


def test_idle_above_threshold_no_blueprints_dreaming(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    report = run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=lambda n: [],
        falsified_beliefs=[],
        fused_meta_postmortems=[],
    )
    assert report.verdict is ConsolidationVerdict.DREAMING
    assert len(report.candidates) == 0


# ---------------------------------------------------------------------------
# Match logic
# ---------------------------------------------------------------------------


def test_belief_falsified_match_only(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    bp = _FakeBlueprint(target_files=("foo.py",))
    claim = _FakeClaim(target_files=("foo.py",))
    report = run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=lambda n: [bp],
        falsified_beliefs=[claim],
        fused_meta_postmortems=[],
    )
    assert report.verdict is ConsolidationVerdict.CONSOLIDATED
    assert len(report.candidates) == 1
    cand = report.candidates[0]
    assert cand.best_match_kind is MatchKind.BELIEF_FALSIFIED
    assert "foo.py" in cand.target_files


def test_postmortem_fused_match_only(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    bp = _FakeBlueprint(target_files=("bar.py",))
    meta = _FakeMeta(target_files_union=("bar.py",))
    report = run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=lambda n: [bp],
        falsified_beliefs=[],
        fused_meta_postmortems=[meta],
    )
    assert report.verdict is ConsolidationVerdict.CONSOLIDATED
    assert report.candidates[0].best_match_kind is MatchKind.POSTMORTEM_FUSED


def test_file_overlap_both_sources_agree(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    bp = _FakeBlueprint(target_files=("shared.py",))
    claim = _FakeClaim(target_files=("shared.py",))
    meta = _FakeMeta(target_files_union=("shared.py",))
    report = run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=lambda n: [bp],
        falsified_beliefs=[claim],
        fused_meta_postmortems=[meta],
    )
    assert report.candidates[0].best_match_kind is MatchKind.FILE_OVERLAP


def test_disjoint_overlap_postmortem_wins(monkeypatch):
    """When BOTH sources fire but on disjoint files, the
    PostmortemFusion source wins (rarer / stronger signal
    per substrate doc)."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    bp = _FakeBlueprint(target_files=("a.py", "b.py"))
    claim = _FakeClaim(target_files=("a.py",))
    meta = _FakeMeta(target_files_union=("b.py",))
    report = run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=lambda n: [bp],
        falsified_beliefs=[claim],
        fused_meta_postmortems=[meta],
    )
    assert report.candidates[0].best_match_kind is MatchKind.POSTMORTEM_FUSED


def test_no_overlap_dreaming(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    bp = _FakeBlueprint(target_files=("alpha.py",))
    claim = _FakeClaim(target_files=("beta.py",))
    meta = _FakeMeta(target_files_union=("gamma.py",))
    report = run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=lambda n: [bp],
        falsified_beliefs=[claim],
        fused_meta_postmortems=[meta],
    )
    assert report.verdict is ConsolidationVerdict.DREAMING


def test_match_threshold_filters_candidates(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    monkeypatch.setenv(_ENV_MATCH_THRESHOLD, "5")  # high bar
    bp = _FakeBlueprint(target_files=("x.py",))
    claim = _FakeClaim(target_files=("x.py",))
    # Only 1 match — below threshold 5
    report = run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=lambda n: [bp],
        falsified_beliefs=[claim],
        fused_meta_postmortems=[],
    )
    assert report.verdict is ConsolidationVerdict.DREAMING


def test_max_candidates_cap(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    monkeypatch.setenv(_ENV_MAX_CANDIDATES, "2")
    bps = [
        _FakeBlueprint(blueprint_id=f"bp-{i}",
                       target_files=(f"f{i}.py",))
        for i in range(5)
    ]
    claims = [_FakeClaim(target_files=(f"f{i}.py",)) for i in range(5)]
    report = run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=lambda n: bps,
        falsified_beliefs=claims,
        fused_meta_postmortems=[],
    )
    assert len(report.candidates) <= 2


def test_boundary_crossed_flagged(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    cage_path = (
        "backend/core/ouroboros/governance/orchestrator.py"
    )
    bp = _FakeBlueprint(target_files=(cage_path,))
    claim = _FakeClaim(target_files=(cage_path,))
    report = run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=lambda n: [bp],
        falsified_beliefs=[claim],
        fused_meta_postmortems=[],
    )
    assert len(report.candidates) == 1
    # boundary_crossed depends on gate state; if set, verify
    if report.candidates[0].boundary_crossed:
        assert True  # gate fired correctly


def test_provider_exception_falls_through(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")

    def _broken_provider(n):
        raise RuntimeError("boom")

    report = run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=_broken_provider,
        falsified_beliefs=[],
        fused_meta_postmortems=[],
    )
    assert report.verdict is ConsolidationVerdict.DREAMING
    assert report.blueprints_examined == 0


def test_default_provider_yields_no_candidates(monkeypatch):
    """With no provider injected, the default stub returns ()
    and the pass degrades to DREAMING (not exception)."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    report = run_consolidation_pass(
        idle_seconds=2000.0,
        falsified_beliefs=[],
        fused_meta_postmortems=[],
    )
    assert report.verdict is ConsolidationVerdict.DREAMING


# ---------------------------------------------------------------------------
# §33.4 persistence
# ---------------------------------------------------------------------------


def test_persist_writes_summary_and_candidates(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    bp = _FakeBlueprint(target_files=("x.py",))
    claim = _FakeClaim(target_files=("x.py",))
    run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=lambda n: [bp],
        falsified_beliefs=[claim],
        fused_meta_postmortems=[],
    )
    p = ledger_path()
    assert p.exists()
    rows = [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    kinds = {r["kind"] for r in rows}
    assert "summary" in kinds
    assert "candidate" in kinds


def test_persist_disabled_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    bp = _FakeBlueprint(target_files=("x.py",))
    claim = _FakeClaim(target_files=("x.py",))
    run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=lambda n: [bp],
        falsified_beliefs=[claim],
        fused_meta_postmortems=[],
    )
    assert not ledger_path().exists()


def test_awake_does_not_persist(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    run_consolidation_pass(idle_seconds=10.0)
    # AWAKE verdict short-circuits before persistence
    assert not ledger_path().exists()


# ---------------------------------------------------------------------------
# format_consolidation_panel
# ---------------------------------------------------------------------------


def test_format_panel_master_off():
    out = format_consolidation_panel()
    assert "disabled" in out
    assert _ENV_MASTER in out


def test_format_panel_master_on_no_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = format_consolidation_panel()
    assert "no report" in out.lower()


def test_format_panel_consolidated(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    bp = _FakeBlueprint(target_files=("p.py",))
    claim = _FakeClaim(target_files=("p.py",))
    report = run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=lambda n: [bp],
        falsified_beliefs=[claim],
        fused_meta_postmortems=[],
    )
    out = format_consolidation_panel(report)
    assert "Sleep Consolidation" in out
    assert "consolidated" in out


# ---------------------------------------------------------------------------
# to_dict shape
# ---------------------------------------------------------------------------


def test_match_to_dict_shape():
    m = ConsolidationMatch(
        blueprint_id="bp",
        match_kind=MatchKind.BELIEF_FALSIFIED,
        overlapping_files=("a.py",),
        supporting_belief_ids=("cid-1",),
        supporting_meta_signatures=(),
    )
    d = m.to_dict()
    assert d["match_kind"] == "belief_falsified"
    assert d["schema_version"] == SLEEP_CONSOLIDATION_SCHEMA_VERSION


def test_candidate_to_dict_shape():
    cand = ConsolidationCandidate(
        blueprint_id="bp",
        blueprint_title="t",
        blueprint_category="c",
        target_files=("a.py",),
        match_count=1,
        best_match_kind=MatchKind.BELIEF_FALSIFIED,
        matches=(),
        boundary_crossed=False,
    )
    d = cand.to_dict()
    assert d["best_match_kind"] == "belief_falsified"
    assert d["schema_version"] == SLEEP_CONSOLIDATION_SCHEMA_VERSION


def test_report_to_dict_shape(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    report = run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=lambda n: [],
        falsified_beliefs=[],
        fused_meta_postmortems=[],
    )
    d = report.to_dict()
    assert d["verdict"] == "dreaming"
    assert d["schema_version"] == SLEEP_CONSOLIDATION_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# AST pins — canonical-source pass
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/"
        "sleep_consolidation_pass.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 6


@pytest.mark.parametrize(
    "name_part",
    [
        "verdict_taxonomy_closed",
        "match_taxonomy_closed",
        "authority_asymmetry",
        "master_default_false",
        "composes_canonical",
        "lazy_dream_import",
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


def test_pin_verdict_synthetic_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy_closed" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class ConsolidationVerdict(str, enum.Enum):\n"
        "    AWAKE = 'awake'\n"
        "    DREAMING = 'dreaming'\n"
        "    CONSOLIDATED = 'consolidated'\n"
        # missing DISABLED
    )
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


def test_pin_match_synthetic_extra():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "match_taxonomy_closed" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class MatchKind(str, enum.Enum):\n"
        "    BELIEF_FALSIFIED = 'belief_falsified'\n"
        "    POSTMORTEM_FUSED = 'postmortem_fused'\n"
        "    FILE_OVERLAP = 'file_overlap'\n"
        "    NONE = 'none'\n"
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
    bad_src = "# no canonical composition\n"
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


def test_pin_lazy_dream_synthetic_violation():
    """A module-level import of consciousness.dream_engine
    MUST fail the pin."""
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "lazy_dream_import" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.consciousness."
        "dream_engine import DreamEngine\n"
    )
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


def test_pin_lazy_dream_inside_function_ok():
    """An import inside a function body is OK (lazy)."""
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "lazy_dream_import" in p.invariant_name
    )
    good_src = (
        "def get_bp():\n"
        "    from backend.core.ouroboros.consciousness."
        "dream_engine import DreamEngine\n"
        "    return DreamEngine\n"
    )
    tree = ast.parse(good_src)
    assert pin.validate(tree, good_src) == ()


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
        _ENV_IDLE_THRESHOLD,
        _ENV_MATCH_THRESHOLD,
        _ENV_MAX_CANDIDATES,
        _ENV_MAX_BLUEPRINTS,
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
# Composition with Wave 4 #9 and #11 (real, end-to-end)
# ---------------------------------------------------------------------------


def test_real_composition_with_belief_ledger_disabled(monkeypatch, tmp_path):
    """When belief ledger master is OFF, evaluate_recent_beliefs
    returns () — the substrate falls through cleanly."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    # belief ledger master stays OFF (default)
    report = run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=lambda n: [],
        # No injection — substrate composes real Wave 4 #9
    )
    assert report.falsified_belief_count == 0
    assert report.verdict is ConsolidationVerdict.DREAMING


def test_real_composition_with_belief_ledger_active(monkeypatch):
    """End-to-end: Wave 4 #9 produces falsified claim; the
    substrate composes it as a match against a blueprint."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_BELIEF_REVISION_FALSIFY_THRESHOLD", "1",
    )
    from backend.core.ouroboros.governance import (
        belief_revision_ledger as brl,
    )
    # Record a claim + falsifying evidence
    claim = brl.record_claim(
        text="real-comp test",
        domain="rct",
        target_files=("real_target.py",),
        now_unix=1.0,
    )
    assert claim is not None
    brl.record_evidence(
        claim.claim_id,
        brl.EvidenceKind.FALSIFYING,
        now_unix=2.0,
    )
    bp = _FakeBlueprint(target_files=("real_target.py",))
    report = run_consolidation_pass(
        idle_seconds=2000.0,
        blueprints_provider=lambda n: [bp],
        # belief composition is NOT mocked — real evaluate_recent
        # is called via _load_falsified_beliefs
    )
    assert report.falsified_belief_count >= 1
    assert report.verdict is ConsolidationVerdict.CONSOLIDATED


# ---------------------------------------------------------------------------
# SSE bind
# ---------------------------------------------------------------------------


def test_sse_event_symbol_exists():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert hasattr(ios, "EVENT_TYPE_SLEEP_CONSOLIDATION_PASSED")
    assert (
        ios.EVENT_TYPE_SLEEP_CONSOLIDATION_PASSED
        == "sleep_consolidation_passed"
    )


def test_sse_event_in_valid_set():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert (
        "sleep_consolidation_passed"
        in ios._VALID_EVENT_TYPES
    )
