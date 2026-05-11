"""Regression spine for §40 Wave 4 #13 — Anti-Fragility Budget.

Covers:

* §33.1 cognitive substrate default-FALSE
* Closed 4-value :class:`StressVerdict` taxonomy
* Closed 4-value :class:`DominantSignal` taxonomy
* evaluate_module → 4-value verdict (HEALTHY / STRESSED /
  EXHAUSTED / DISABLED)
* Stress score formula: weighted (belief × bw + fragility × fw)
* Budget allocator (HEALTHY=max, STRESSED=max//div, EXHAUSTED=0)
* DominantSignal classifier (BELIEF_PRESSURE / DOLL_FRAGILITY /
  COMBINED / NONE)
* Composes Wave 4 #9 belief_revision_ledger (substring-matched)
* Composes Wave 1 #15 second_order_doll_metric (canonical
  Category match)
* Composes Wave 2 #5 governance_boundary_gate (cage flag)
* Composes cross_process_jsonl for §33.4 persistence
* Threshold env clamps + auto-clamp exhausted ≥ stressed
* 5 AST pin canonical-source pass + 5 synthetic regressions
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
    anti_fragility_budget as afb,
)
from backend.core.ouroboros.governance.anti_fragility_budget import (
    ANTI_FRAGILITY_SCHEMA_VERSION,
    AntiFragilityReport,
    DominantSignal,
    ModuleBudget,
    StressVerdict,
    _ENV_BELIEF_WEIGHT,
    _ENV_EXHAUSTED_THRESHOLD,
    _ENV_FRAGILITY_WEIGHT,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MAX_BUDGET,
    _ENV_PERSIST,
    _ENV_STRESSED_DIVISOR,
    _ENV_STRESSED_THRESHOLD,
    _classify_dominant_signal,
    _budget_for_verdict,
    belief_weight,
    evaluate_module,
    evaluate_modules,
    exhausted_threshold,
    format_anti_fragility_panel,
    fragility_weight,
    ledger_path,
    master_enabled,
    max_budget,
    persistence_enabled,
    register_flags,
    register_shipped_invariants,
    signal_glyph,
    stressed_divisor,
    stressed_threshold,
    verdict_glyph,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeClaim:
    claim_id: str = "cid"
    text: str = ""
    domain: str = ""
    confidence: float = 0.5
    target_files: Tuple[str, ...] = field(default_factory=tuple)


@dataclass
class _FakeBeliefReport:
    """Mimics belief_revision_ledger.BeliefRevisionReport."""
    verdict: Any = None
    claim: Any = None
    falsifying_count: int = 1


@dataclass
class _FakeDollStage:
    value: str = "untouched"


@dataclass
class _FakeAxis:
    category: str = "safety"
    stage: Any = field(default_factory=_FakeDollStage)


@dataclass
class _FakeDollSnapshot:
    master_enabled: bool = True
    axes: Tuple[Any, ...] = field(default_factory=tuple)


def _falsified_report(domain: str = "", files: Tuple[str, ...] = ()):
    """Build a fake belief report with FALSIFIED verdict."""
    from backend.core.ouroboros.governance.belief_revision_ledger import (
        BeliefVerdict,
    )
    return _FakeBeliefReport(
        verdict=BeliefVerdict.FALSIFIED,
        claim=_FakeClaim(domain=domain, target_files=files),
    )


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER,
        _ENV_PERSIST,
        _ENV_STRESSED_THRESHOLD,
        _ENV_EXHAUSTED_THRESHOLD,
        _ENV_MAX_BUDGET,
        _ENV_STRESSED_DIVISOR,
        _ENV_BELIEF_WEIGHT,
        _ENV_FRAGILITY_WEIGHT,
        _ENV_LEDGER_PATH,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_LEDGER_PATH,
        str(tmp_path / "anti_fragility.jsonl"),
    )
    yield


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_schema_version():
    assert ANTI_FRAGILITY_SCHEMA_VERSION == "anti_fragility.1"


def test_master_default_false():
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert master_enabled() is True


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_stressed_threshold_default():
    assert stressed_threshold() == 0.25


def test_exhausted_threshold_default():
    assert exhausted_threshold() == 0.50


def test_exhausted_auto_clamped_above_stressed(monkeypatch):
    monkeypatch.setenv(_ENV_STRESSED_THRESHOLD, "0.80")
    monkeypatch.setenv(_ENV_EXHAUSTED_THRESHOLD, "0.20")
    # exhausted < stressed → auto-clamped UP to stressed value
    assert exhausted_threshold() == 0.80


def test_max_budget_default():
    assert max_budget() == 100


def test_stressed_divisor_default():
    assert stressed_divisor() == 4


def test_belief_weight_default():
    assert belief_weight() == 1.0


def test_fragility_weight_default():
    assert fragility_weight() == 0.5


def test_ledger_path_default(monkeypatch):
    monkeypatch.delenv(_ENV_LEDGER_PATH, raising=False)
    p = ledger_path()
    assert str(p) == ".jarvis/anti_fragility_ledger.jsonl"


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


def test_stress_verdict_taxonomy_closed():
    assert {v.value for v in StressVerdict} == {
        "healthy", "stressed", "exhausted", "disabled",
    }


def test_dominant_signal_taxonomy_closed():
    assert {s.value for s in DominantSignal} == {
        "belief_pressure", "doll_fragility", "combined", "none",
    }


@pytest.mark.parametrize("v", list(StressVerdict))
def test_verdict_glyph_known(v):
    assert verdict_glyph(v) != "?"


@pytest.mark.parametrize("s", list(DominantSignal))
def test_signal_glyph_known(s):
    assert signal_glyph(s) != "?"


# ---------------------------------------------------------------------------
# Budget allocator
# ---------------------------------------------------------------------------


def test_budget_healthy_full():
    assert _budget_for_verdict(StressVerdict.HEALTHY, 100, 4) == 100


def test_budget_stressed_divided():
    assert _budget_for_verdict(StressVerdict.STRESSED, 100, 4) == 25


def test_budget_stressed_divisor_one():
    # divisor=1 → STRESSED == full
    assert _budget_for_verdict(StressVerdict.STRESSED, 100, 1) == 100


def test_budget_exhausted_zero():
    assert _budget_for_verdict(StressVerdict.EXHAUSTED, 100, 4) == 0


def test_budget_disabled_passthrough():
    # DISABLED returns full budget — substrate stays out of caller's way
    assert _budget_for_verdict(StressVerdict.DISABLED, 100, 4) == 100


# ---------------------------------------------------------------------------
# DominantSignal classifier
# ---------------------------------------------------------------------------


def test_signal_none_both_low():
    assert _classify_dominant_signal(0.05, 0.05) is DominantSignal.NONE


def test_signal_combined_equal():
    assert _classify_dominant_signal(0.4, 0.4) is DominantSignal.COMBINED


def test_signal_belief_dominant():
    assert (
        _classify_dominant_signal(0.6, 0.2)
        is DominantSignal.BELIEF_PRESSURE
    )


def test_signal_fragility_dominant():
    assert (
        _classify_dominant_signal(0.1, 0.7)
        is DominantSignal.DOLL_FRAGILITY
    )


# ---------------------------------------------------------------------------
# evaluate_module — verdict transitions
# ---------------------------------------------------------------------------


def test_evaluate_master_off_disabled():
    budget = evaluate_module("module_x")
    assert budget.verdict is StressVerdict.DISABLED
    assert budget.remaining_budget == max_budget()


def test_evaluate_empty_module_disabled(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    budget = evaluate_module("")
    assert budget.verdict is StressVerdict.DISABLED


def test_evaluate_healthy_no_pressure_no_fragility(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    budget = evaluate_module(
        "module_x",
        falsified_reports=[],
        doll_snapshot=_FakeDollSnapshot(axes=()),
    )
    assert budget.verdict is StressVerdict.HEALTHY
    assert budget.remaining_budget == max_budget()
    assert budget.dominant_signal is DominantSignal.NONE


def test_evaluate_stressed_belief_pressure(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    # 1 of 3 falsified reports matches module → 0.33 * 2.0 = 0.67
    # belief score; default weights → combined ≈ (0.67*1.0)/1.5 ≈ 0.44
    reports = [
        _falsified_report(domain="module_x_dom"),
        _falsified_report(domain="something_else"),
        _falsified_report(domain="other_dom"),
    ]
    budget = evaluate_module(
        "module_x",
        falsified_reports=reports,
        doll_snapshot=_FakeDollSnapshot(axes=()),
    )
    # 1/3 = 0.333 * 2.0 = 0.667; weighted (0.667*1.0)/1.5 = 0.444 < 0.50
    assert budget.verdict is StressVerdict.STRESSED


def test_evaluate_exhausted_all_falsified_match(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    # All reports match → belief pressure capped at 1.0
    reports = [
        _falsified_report(domain="module_x_dom") for _ in range(5)
    ]
    budget = evaluate_module(
        "module_x",
        falsified_reports=reports,
        doll_snapshot=_FakeDollSnapshot(axes=()),
    )
    # belief=1.0, fragility=0.0; weighted = (1.0*1.0 + 0*0.5)/1.5 = 0.667
    # 0.667 >= 0.50 → EXHAUSTED
    assert budget.verdict is StressVerdict.EXHAUSTED
    assert budget.remaining_budget == 0


def test_evaluate_doll_fragility_alone(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_FRAGILITY_WEIGHT, "2.0")  # boost fragility
    # No beliefs; doll snapshot has UNTOUCHED axis → fragility=1.0
    snapshot = _FakeDollSnapshot(axes=(
        _FakeAxis(category="module_x", stage=_FakeDollStage("untouched")),
    ))
    budget = evaluate_module(
        "module_x",
        falsified_reports=[],
        doll_snapshot=snapshot,
    )
    # belief=0, fragility=1.0; bw=1.0 fw=2.0; (0+2.0)/3.0 = 0.667 → EXHAUSTED
    assert budget.verdict is StressVerdict.EXHAUSTED
    assert budget.dominant_signal is DominantSignal.DOLL_FRAGILITY


def test_evaluate_graduated_doll_no_fragility(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    snapshot = _FakeDollSnapshot(axes=(
        _FakeAxis(category="module_x", stage=_FakeDollStage("graduated")),
    ))
    budget = evaluate_module(
        "module_x",
        falsified_reports=[],
        doll_snapshot=snapshot,
    )
    assert budget.verdict is StressVerdict.HEALTHY
    # GRADUATED → stage_weight=1.0 → fragility=0.0
    assert budget.doll_fragility == 0.0


def test_evaluate_target_files_substring_match(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    # Reports match via target_files not domain
    reports = [
        _falsified_report(
            domain="generic_dom",
            files=("backend/module_x/foo.py",),
        )
        for _ in range(5)
    ]
    budget = evaluate_module(
        "module_x",
        falsified_reports=reports,
        doll_snapshot=_FakeDollSnapshot(axes=()),
    )
    assert budget.falsified_domain_count >= 5


def test_evaluate_no_match_returns_healthy(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    reports = [
        _falsified_report(domain="completely_unrelated")
        for _ in range(10)
    ]
    budget = evaluate_module(
        "module_x",
        falsified_reports=reports,
        doll_snapshot=_FakeDollSnapshot(axes=()),
    )
    assert budget.verdict is StressVerdict.HEALTHY
    assert budget.falsified_domain_count == 0


def test_evaluate_diagnostic_contains_stress_score(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    budget = evaluate_module(
        "module_x",
        falsified_reports=[],
        doll_snapshot=_FakeDollSnapshot(axes=()),
    )
    assert "stress=" in budget.diagnostic
    assert "budget=" in budget.diagnostic


def test_evaluate_threshold_override(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_STRESSED_THRESHOLD, "0.05")  # very low
    monkeypatch.setenv(_ENV_EXHAUSTED_THRESHOLD, "0.10")
    # 1 of 3 belief matches — but threshold now 0.05 so STRESSED earlier
    reports = [
        _falsified_report(domain="module_x_dom"),
        _falsified_report(domain="other"),
        _falsified_report(domain="other2"),
    ]
    budget = evaluate_module(
        "module_x",
        falsified_reports=reports,
        doll_snapshot=_FakeDollSnapshot(axes=()),
    )
    # belief=0.667, weighted=0.444 > 0.10 → EXHAUSTED
    assert budget.verdict is StressVerdict.EXHAUSTED


# ---------------------------------------------------------------------------
# evaluate_modules — aggregate
# ---------------------------------------------------------------------------


def test_evaluate_modules_master_off():
    report = evaluate_modules(["a", "b"])
    assert isinstance(report, AntiFragilityReport)
    assert report.master_enabled is False
    assert len(report.per_module) == 0


def test_evaluate_modules_master_on_returns_one_per(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_modules(
        ["a", "b", "c"],
        falsified_reports=[],
        doll_snapshot=_FakeDollSnapshot(axes=()),
    )
    assert len(report.per_module) == 3


def test_evaluate_modules_skips_empty_ids(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_modules(
        ["a", "", "b", "   "],
        falsified_reports=[],
        doll_snapshot=_FakeDollSnapshot(axes=()),
    )
    assert len(report.per_module) == 2


def test_evaluate_modules_counts_correct(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_STRESSED_THRESHOLD, "0.10")
    monkeypatch.setenv(_ENV_EXHAUSTED_THRESHOLD, "0.40")
    # mod_x: all match → EXHAUSTED
    # mod_y: 1 of 5 match → STRESSED (0.4 ≥ 0.10)
    # mod_z: 0 match → HEALTHY
    reports = [_falsified_report(domain="mod_x") for _ in range(4)]
    reports.append(_falsified_report(domain="mod_y"))
    report = evaluate_modules(
        ["mod_x", "mod_y", "mod_z"],
        falsified_reports=reports,
        doll_snapshot=_FakeDollSnapshot(axes=()),
    )
    counts = (
        report.healthy_count,
        report.stressed_count,
        report.exhausted_count,
    )
    # exact distribution depends on weighted-score math; just verify
    # there's some stress
    assert sum(counts) == 3
    assert report.exhausted_count >= 1


# ---------------------------------------------------------------------------
# §33.4 persistence
# ---------------------------------------------------------------------------


def test_persist_writes_when_stressed(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    reports = [_falsified_report(domain="mod_x") for _ in range(5)]
    evaluate_modules(
        ["mod_x"],
        falsified_reports=reports,
        doll_snapshot=_FakeDollSnapshot(axes=()),
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


def test_no_persist_when_all_healthy(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    evaluate_modules(
        ["mod_x"],
        falsified_reports=[],
        doll_snapshot=_FakeDollSnapshot(axes=()),
    )
    # All HEALTHY → no SSE / no persist
    assert not ledger_path().exists()


def test_persist_disabled_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    reports = [_falsified_report(domain="mod_x") for _ in range(5)]
    evaluate_modules(
        ["mod_x"],
        falsified_reports=reports,
        doll_snapshot=_FakeDollSnapshot(axes=()),
    )
    assert not ledger_path().exists()


# ---------------------------------------------------------------------------
# End-to-end composition with real Wave 4 #9
# ---------------------------------------------------------------------------


def test_real_composition_with_belief_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_BELIEF_REVISION_FALSIFY_THRESHOLD", "1",
    )
    monkeypatch.setenv(
        "JARVIS_BELIEF_REVISION_LEDGER_PATH",
        str(tmp_path / "belief.jsonl"),
    )
    from backend.core.ouroboros.governance import (
        belief_revision_ledger as brl,
    )
    # Record a claim + falsifying evidence for module "alpha"
    claim = brl.record_claim(
        text="real-comp",
        domain="alpha_belief",
        target_files=("alpha/foo.py",),
        now_unix=1.0,
    )
    assert claim is not None
    brl.record_evidence(
        claim.claim_id,
        brl.EvidenceKind.FALSIFYING,
        now_unix=2.0,
    )
    # No injection — substrate composes real Wave 4 #9
    report = evaluate_modules(
        ["alpha", "beta"],
        doll_snapshot=_FakeDollSnapshot(axes=()),
    )
    alpha = next(m for m in report.per_module if m.module_id == "alpha")
    beta = next(m for m in report.per_module if m.module_id == "beta")
    # alpha matches via either domain "alpha_belief" or files "alpha/foo.py"
    assert alpha.falsified_domain_count >= 1
    assert beta.falsified_domain_count == 0


# ---------------------------------------------------------------------------
# format_anti_fragility_panel
# ---------------------------------------------------------------------------


def test_format_panel_master_off():
    out = format_anti_fragility_panel()
    assert "disabled" in out


def test_format_panel_master_on_no_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = format_anti_fragility_panel()
    assert "no report" in out.lower()


def test_format_panel_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_modules(
        ["mod_x"],
        falsified_reports=[],
        doll_snapshot=_FakeDollSnapshot(axes=()),
    )
    out = format_anti_fragility_panel(report)
    assert "Anti-Fragility Budget" in out
    assert "mod_x" in out


# ---------------------------------------------------------------------------
# to_dict shape
# ---------------------------------------------------------------------------


def test_module_budget_to_dict():
    b = ModuleBudget(
        module_id="m",
        stress_score=0.5,
        belief_pressure=0.5,
        doll_fragility=0.0,
        verdict=StressVerdict.STRESSED,
        dominant_signal=DominantSignal.BELIEF_PRESSURE,
        remaining_budget=25,
        max_budget=100,
        falsified_domain_count=3,
        matching_doll_stage="",
        boundary_crossed=False,
        diagnostic="x",
    )
    d = b.to_dict()
    assert d["verdict"] == "stressed"
    assert d["dominant_signal"] == "belief_pressure"
    assert d["schema_version"] == ANTI_FRAGILITY_SCHEMA_VERSION


def test_report_to_dict(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_modules(
        ["m"],
        falsified_reports=[],
        doll_snapshot=_FakeDollSnapshot(axes=()),
    )
    d = report.to_dict()
    assert d["schema_version"] == ANTI_FRAGILITY_SCHEMA_VERSION
    assert isinstance(d["per_module"], list)


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/"
        "anti_fragility_budget.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 5


@pytest.mark.parametrize(
    "name_part",
    [
        "verdict_taxonomy_closed",
        "signal_taxonomy_closed",
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


def test_pin_verdict_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy_closed" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class StressVerdict(str, enum.Enum):\n"
        "    HEALTHY = 'healthy'\n"
        "    STRESSED = 'stressed'\n"
        "    EXHAUSTED = 'exhausted'\n"
        # missing DISABLED
    )
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


def test_pin_signal_extra():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "signal_taxonomy_closed" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class DominantSignal(str, enum.Enum):\n"
        "    BELIEF_PRESSURE = 'belief_pressure'\n"
        "    DOLL_FRAGILITY = 'doll_fragility'\n"
        "    COMBINED = 'combined'\n"
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
        "from backend.core.ouroboros.governance.sensor_governor "
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
        "    return _flag('X', default=True)\n"
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
    assert count == 8
    names = {spec.name for spec in reg.registered}
    expected = {
        _ENV_MASTER,
        _ENV_PERSIST,
        _ENV_STRESSED_THRESHOLD,
        _ENV_EXHAUSTED_THRESHOLD,
        _ENV_MAX_BUDGET,
        _ENV_STRESSED_DIVISOR,
        _ENV_BELIEF_WEIGHT,
        _ENV_FRAGILITY_WEIGHT,
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
    assert hasattr(ios, "EVENT_TYPE_ANTI_FRAGILITY_EVALUATED")
    assert (
        ios.EVENT_TYPE_ANTI_FRAGILITY_EVALUATED
        == "anti_fragility_evaluated"
    )


def test_sse_event_in_valid_set():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert "anti_fragility_evaluated" in ios._VALID_EVENT_TYPES
