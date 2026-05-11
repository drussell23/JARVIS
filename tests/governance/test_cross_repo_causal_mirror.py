"""Regression spine for §40 Wave 5 #20 — Cross-Repo Causal Mirror."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Tuple

import pytest


from backend.core.ouroboros.governance import (
    cross_repo_causal_mirror as crm,
)
from backend.core.ouroboros.governance.cross_repo_causal_mirror import (
    CROSS_REPO_MIRROR_SCHEMA_VERSION,
    CausalSignal,
    CrossRepoMirrorReport,
    MirrorCorrelation,
    MirrorVerdict,
    _ENV_FORCE_TRIGGER,
    _ENV_GIT_TIMEOUT_S,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MAX_COMMITS,
    _ENV_MAX_POSTMORTEMS,
    _ENV_MIRROR_PATH,
    _ENV_PERSIST,
    _build_correlations,
    force_trigger_enabled,
    format_mirror_panel,
    ledger_path,
    master_enabled,
    max_commits_to_scan,
    max_postmortems_to_scan,
    persistence_enabled,
    register_flags,
    register_shipped_invariants,
    scan_mirror_correlations,
    signal_glyph,
    verdict_glyph,
)


@dataclass
class _FakePostmortem:
    op_id: str
    target_files: Tuple[str, ...] = field(default_factory=tuple)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER, _ENV_PERSIST, _ENV_FORCE_TRIGGER,
        _ENV_MIRROR_PATH, _ENV_MAX_COMMITS,
        _ENV_MAX_POSTMORTEMS, _ENV_GIT_TIMEOUT_S,
        _ENV_LEDGER_PATH,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_LEDGER_PATH, str(tmp_path / "mirror.jsonl"),
    )
    yield


# Defaults / taxonomies


def test_schema():
    assert CROSS_REPO_MIRROR_SCHEMA_VERSION == "cross_repo_mirror.1"


def test_master_default_false():
    assert master_enabled() is False


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_force_trigger_default_false():
    assert force_trigger_enabled() is False


def test_max_commits_default():
    assert max_commits_to_scan() == 50


def test_max_postmortems_default():
    assert max_postmortems_to_scan() == 30


def test_verdict_taxonomy_closed():
    assert {v.value for v in MirrorVerdict} == {
        "trigger_not_met", "no_mirror_detected",
        "mirror_found", "disabled",
    }


def test_signal_taxonomy_closed():
    assert {s.value for s in CausalSignal} == {
        "shared_path", "commit_correlation",
        "postmortem_overlap", "none",
    }


@pytest.mark.parametrize("v", list(MirrorVerdict))
def test_verdict_glyph(v):
    assert verdict_glyph(v) != "?"


@pytest.mark.parametrize("s", list(CausalSignal))
def test_signal_glyph(s):
    assert signal_glyph(s) != "?"


# _build_correlations


def test_build_correlations_empty():
    assert _build_correlations([], []) == ()


def test_build_correlations_overlap():
    commits = (
        ("sha1", "fix bug", ("path/a.py", "path/b.py")),
    )
    pms = [
        _FakePostmortem(op_id="op-1", target_files=("path/a.py",)),
    ]
    out = _build_correlations(commits, pms)
    assert len(out) == 1
    assert out[0].overlapping_files == ("path/a.py",)
    assert "op-1" in out[0].overlapping_postmortem_op_ids


def test_build_correlations_no_overlap():
    commits = (
        ("sha1", "fix", ("nothing.py",)),
    )
    pms = [
        _FakePostmortem(op_id="op-1", target_files=("other.py",)),
    ]
    assert _build_correlations(commits, pms) == ()


def test_build_correlations_multiple_postmortems_per_file():
    commits = (
        ("sha1", "fix", ("shared.py",)),
    )
    pms = [
        _FakePostmortem(op_id="op-1", target_files=("shared.py",)),
        _FakePostmortem(op_id="op-2", target_files=("shared.py",)),
    ]
    out = _build_correlations(commits, pms)
    assert len(out) == 1
    assert set(out[0].overlapping_postmortem_op_ids) == {"op-1", "op-2"}


# scan_mirror_correlations


def test_scan_master_off_disabled():
    report = scan_mirror_correlations(trigger_override=True)
    assert report.master_enabled is False
    assert report.verdict is MirrorVerdict.DISABLED


def test_scan_trigger_not_met(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = scan_mirror_correlations(trigger_override=False)
    assert report.verdict is MirrorVerdict.TRIGGER_NOT_MET


def test_scan_no_mirror_path(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_FORCE_TRIGGER, "true")
    report = scan_mirror_correlations(trigger_override=True)
    assert report.verdict is MirrorVerdict.NO_MIRROR_DETECTED


def test_scan_mirror_found_correlation(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    commits = (
        ("sha1", "subject", ("alpha.py",)),
    )
    pms = [
        _FakePostmortem(op_id="op-1", target_files=("alpha.py",)),
    ]
    report = scan_mirror_correlations(
        mirror_commits_override=commits,
        postmortems_override=pms,
        trigger_override=True,
    )
    assert report.verdict is MirrorVerdict.MIRROR_FOUND
    assert len(report.correlations) == 1


def test_scan_no_correlations_with_commits(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    commits = (
        ("sha1", "subject", ("alpha.py",)),
    )
    pms = [
        _FakePostmortem(op_id="op-1", target_files=("beta.py",)),
    ]
    report = scan_mirror_correlations(
        mirror_commits_override=commits,
        postmortems_override=pms,
        trigger_override=True,
    )
    assert report.verdict is MirrorVerdict.NO_MIRROR_DETECTED


def test_scan_real_force_trigger(monkeypatch):
    """Force-trigger but no real mirror configured → NO_MIRROR_DETECTED."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_FORCE_TRIGGER, "true")
    report = scan_mirror_correlations()
    # Without override, real subprocess + real postmortem_recall;
    # no mirror path → NO_MIRROR_DETECTED.
    assert report.verdict in (
        MirrorVerdict.NO_MIRROR_DETECTED,
        # If real git detects multi-remote, that's also fine.
        MirrorVerdict.MIRROR_FOUND,
    )


# Persistence


def test_persist_mirror_found(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    commits = (
        ("sha1", "subject", ("alpha.py",)),
    )
    pms = [
        _FakePostmortem(op_id="op-1", target_files=("alpha.py",)),
    ]
    scan_mirror_correlations(
        mirror_commits_override=commits,
        postmortems_override=pms,
        trigger_override=True,
    )
    assert ledger_path().exists()


def test_persist_no_correlation_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    scan_mirror_correlations(trigger_override=False)
    assert not ledger_path().exists()


def test_persist_disabled(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    commits = (("sha1", "subject", ("alpha.py",)),)
    pms = [
        _FakePostmortem(op_id="op-1", target_files=("alpha.py",)),
    ]
    scan_mirror_correlations(
        mirror_commits_override=commits,
        postmortems_override=pms,
        trigger_override=True,
    )
    assert not ledger_path().exists()


# Renderer


def test_format_panel_master_off():
    assert "disabled" in format_mirror_panel()


def test_format_panel_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = scan_mirror_correlations(
        trigger_override=False,
    )
    out = format_mirror_panel(report)
    assert "Cross-Repo" in out


# to_dict


def test_correlation_to_dict():
    c = MirrorCorrelation(
        mirror_commit_sha="abc",
        mirror_commit_subject="fix",
        mirror_files=("a.py",),
        overlapping_postmortem_op_ids=("op",),
        overlapping_files=("a.py",),
        dominant_signal=CausalSignal.POSTMORTEM_OVERLAP,
        boundary_crossed=False,
    )
    d = c.to_dict()
    assert d["dominant_signal"] == "postmortem_overlap"
    assert d["schema_version"] == CROSS_REPO_MIRROR_SCHEMA_VERSION


def test_report_to_dict():
    r = CrossRepoMirrorReport(
        evaluated_at_unix=1.0, master_enabled=True,
        verdict=MirrorVerdict.MIRROR_FOUND, trigger_met=True,
        mirror_path="/x", mirror_commits_scanned=1,
        postmortems_scanned=1, correlations=(),
        diagnostic="x", elapsed_s=0.0,
    )
    d = r.to_dict()
    assert d["verdict"] == "mirror_found"
    assert d["schema_version"] == CROSS_REPO_MIRROR_SCHEMA_VERSION


# AST pins


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/"
        "cross_repo_causal_mirror.py",
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
def test_pin_canonical(_canonical, name_part):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(p for p in pins if name_part in p.invariant_name)
    assert pin.validate(tree, src) == ()


def test_pin_verdict_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy_closed" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class MirrorVerdict(str, enum.Enum):\n"
        "    TRIGGER_NOT_MET = 'trigger_not_met'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


# Flags + SSE


class _FakeRegistry:
    def __init__(self):
        self.registered: List[Any] = []

    def register(self, spec):
        self.registered.append(spec)


def test_flag_seed_count():
    reg = _FakeRegistry()
    count = register_flags(reg)
    assert count == 7


def test_flag_master_default_false():
    reg = _FakeRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered if s.name == _ENV_MASTER
    )
    assert master.default is False


def test_sse_event_exists():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert (
        ios.EVENT_TYPE_CROSS_REPO_MIRROR_FOUND
        == "cross_repo_mirror_found"
    )
    assert "cross_repo_mirror_found" in ios._VALID_EVENT_TYPES
