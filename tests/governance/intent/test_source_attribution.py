"""Regression spine — Slice 6 Task 2: the deterministic AST test→source
attribution bridge. Every case here maps to a mandate: no path heuristics
(a tmp repo whose layout deliberately breaks tests/→src/ mirroring),
alias/relative/indirect handling, multi-source bounding, and fail-fast
on unresolvable attribution."""
from __future__ import annotations

import json
import textwrap

import pytest

from backend.core.ouroboros.governance.intent.test_source_attribution import (
    Attribution,
    AttributionUnresolved,
    attribute_test_to_sources,
    attribution_enabled,
    unattributed_test_scope_violation,
)


@pytest.fixture()
def repo(tmp_path):
    """A tmp repo whose test path deliberately does NOT mirror the source
    path (mandate 1: any tests/foo→src/foo heuristic would fail here)."""
    src = tmp_path / "backend" / "core" / "widgets"
    src.mkdir(parents=True)
    (tmp_path / "backend" / "__init__.py").write_text("")
    (tmp_path / "backend" / "core" / "__init__.py").write_text("")
    (src / "__init__.py").write_text("")
    (src / "gadget.py").write_text("def spin():\n    return 1\n")
    (src / "helper.py").write_text("def aid():\n    return 2\n")
    tdir = tmp_path / "tests" / "unit_checks"   # ≠ backend/core/widgets
    tdir.mkdir(parents=True)
    return tmp_path, tdir


def _write_test(tdir, body: str, name: str = "test_gadget.py"):
    p = tdir / name
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_direct_from_import_resolves(repo, monkeypatch) -> None:
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        from backend.core.widgets.gadget import spin
        def test_spin():
            assert spin() == 1
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert attr.source_loci == ("backend/core/widgets/gadget.py",)
    assert attr.test_locus == "tests/unit_checks/test_gadget.py"
    assert attr.method == "direct_import"


def test_aliased_module_import_resolves(repo, monkeypatch) -> None:
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        import backend.core.widgets.gadget as g
        def test_spin():
            assert g.spin() == 1
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert "backend/core/widgets/gadget.py" in attr.source_loci


def test_multi_source_carries_all_bounded(repo, monkeypatch) -> None:
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    monkeypatch.setenv("JARVIS_ATTRIBUTION_MAX_SOURCE_FILES", "8")
    tf = _write_test(tdir, """
        from backend.core.widgets.gadget import spin
        from backend.core.widgets.helper import aid
        def test_both():
            assert spin() + aid() == 3
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert set(attr.source_loci) == {
        "backend/core/widgets/gadget.py",
        "backend/core/widgets/helper.py",
    }


def test_max_source_cap_enforced(repo, monkeypatch) -> None:
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    monkeypatch.setenv("JARVIS_ATTRIBUTION_MAX_SOURCE_FILES", "1")
    tf = _write_test(tdir, """
        from backend.core.widgets.gadget import spin
        from backend.core.widgets.helper import aid
        def test_both():
            assert spin() + aid() == 3
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert len(attr.source_loci) == 1


def test_patch_target_secondary_signal(repo, monkeypatch) -> None:
    """mock.patch target strings recover indirection (the ~17% class)."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        from unittest import mock
        def test_spun():
            with mock.patch("backend.core.widgets.gadget.spin", return_value=9):
                assert True
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert "backend/core/widgets/gadget.py" in attr.source_loci
    assert "patch_target" in attr.evidence_kinds


def test_traceback_frames_rank_first(repo, monkeypatch) -> None:
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        from backend.core.widgets.gadget import spin
        from backend.core.widgets.helper import aid
        def test_both():
            assert spin() + aid() == 3
    """)
    attr = attribute_test_to_sources(
        tf, repo_root=str(root),
        traceback_frames=("backend/core/widgets/helper.py",),
    )
    assert attr.source_loci[0] == "backend/core/widgets/helper.py"


def test_test_infra_imports_excluded(repo, monkeypatch) -> None:
    """Importing a sibling test helper must NOT attribute to test infra —
    classification is config-driven via JARVIS_TEST_DIR_NAMES (mandate 1:
    no hardcoded directory assumption)."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    (tdir / "__init__.py").write_text("")
    (root / "tests" / "__init__.py").write_text("")
    (tdir / "helpers.py").write_text("X = 1\n")
    tf = _write_test(tdir, """
        from tests.unit_checks.helpers import X
        from backend.core.widgets.gadget import spin
        def test_spin():
            assert spin() == X
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert attr.source_loci == ("backend/core/widgets/gadget.py",)


def test_unresolved_no_first_party_imports(repo, monkeypatch) -> None:
    """Fail-fast (mandate 4): stdlib-only test → typed error, never a
    silent test-file-scoped fallback."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        import os
        def test_env():
            assert os.sep
    """)
    with pytest.raises(AttributionUnresolved) as exc:
        attribute_test_to_sources(tf, repo_root=str(root))
    assert exc.value.reason == "no_first_party_source_imports"


def test_unresolved_parse_error(repo, monkeypatch) -> None:
    root, tdir = repo
    tf = _write_test(tdir, "def broken(:\n")
    with pytest.raises(AttributionUnresolved) as exc:
        attribute_test_to_sources(tf, repo_root=str(root))
    assert exc.value.reason == "parse_error"


def test_unresolved_missing_file(repo) -> None:
    root, _ = repo
    with pytest.raises(AttributionUnresolved) as exc:
        attribute_test_to_sources("tests/nope/test_ghost.py", repo_root=str(root))
    assert exc.value.reason == "test_file_missing"


def test_deterministic_across_calls(repo, monkeypatch) -> None:
    """Same inputs → identical output tuple (mandate 1: deterministic)."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        from backend.core.widgets.helper import aid
        from backend.core.widgets.gadget import spin
        def test_both():
            assert spin() + aid() == 3
    """)
    a = attribute_test_to_sources(tf, repo_root=str(root))
    b = attribute_test_to_sources(tf, repo_root=str(root))
    assert a == b


def test_master_switch(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED", "false")
    assert attribution_enabled() is False
    monkeypatch.delenv("JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED", raising=False)
    assert attribution_enabled() is True


# ---- the Task-5 gate predicate (pure function, unit-tested here) ----

def _ev(status: str, test_locus: str = "tests/unit_checks/test_gadget.py") -> str:
    return json.dumps({"attribution": {
        "schema_version": 1, "status": status, "test_locus": test_locus,
        "source_loci": [], "method": "", "reason": "no_first_party_source_imports",
    }})


def test_gate_fires_on_unresolved_test_only_mutation(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    msg = unattributed_test_scope_violation(
        _ev("unresolved"), ["tests/unit_checks/test_gadget.py"],
    )
    assert msg is not None and "unresolved" in msg


def test_gate_silent_when_resolved(monkeypatch) -> None:
    assert unattributed_test_scope_violation(
        _ev("resolved"), ["tests/unit_checks/test_gadget.py"],
    ) is None


def test_gate_silent_when_candidate_touches_source(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    assert unattributed_test_scope_violation(
        _ev("unresolved"),
        ["backend/core/widgets/gadget.py", "tests/unit_checks/test_gadget.py"],
    ) is None


def test_gate_fail_soft_on_malformed_evidence() -> None:
    assert unattributed_test_scope_violation("{not json", ["x.py"]) is None
    assert unattributed_test_scope_violation("", ["x.py"]) is None
