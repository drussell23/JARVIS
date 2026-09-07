"""Declared-symbol contract: signed intent outranks the model's "already done".

Root cause covered: the model returned 2b.1-noop for a goal that DECLARES
symbols which do not exist yet; the pipeline honoured the no-op and the
goal stayed "in flight" until the soak idled out.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import declared_symbols as D


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(D._ENV_ENABLED, raising=False)


def test_defined_names_include_methods_and_classes():
    names = D.defined_names("class A:\n    def m(self): ...\nasync def f(): ...\n")
    assert names == frozenset({"A", "m", "f"})
    assert D.defined_names("def broken(:\n") == frozenset()


def test_missing_declared_symbols_checks_target_files_on_disk(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_old(): ...\n")
    files = ("tests/test_a.py", "tests/missing.py")
    assert D.missing_declared_symbols(("test_old", "test_new"), files, tmp_path) == ("test_new",)
    assert D.missing_declared_symbols((), files, tmp_path) == ()
    assert D.missing_declared_symbols(("", "  "), files, tmp_path) == ()
    monkeypatch.setenv(D._ENV_ENABLED, "false")
    assert D.missing_declared_symbols(("test_new",), files, tmp_path) == ()


def test_symbols_missing_from_candidate_supports_both_candidate_shapes():
    multi = {"files": [{"file_path": "a.py", "full_content": "def test_x(): ...\n"}, {"file_path": "b.py", "full_content": "def test_y(): ...\n"}]}
    assert D.symbols_missing_from_candidate(("test_x", "test_y"), multi) == ()
    assert D.symbols_missing_from_candidate(("test_x", "test_z"), multi) == ("test_z",)
    single = {"file_path": "a.py", "full_content": "class C:\n    def test_m(self): ...\n"}
    assert D.symbols_missing_from_candidate(("test_m",), single) == ()
    assert D.symbols_missing_from_candidate(("test_m",), {"file_path": "a.py", "diff": "+x"}) == ()   # diff candidates judged by tests
    assert D.symbols_missing_from_candidate((), single) == ()


def test_refusal_feedback_names_symbols_and_files():
    fb = D.refusal_feedback(("test_new",), ("tests/test_a.py",))
    assert "NO-OP REFUSED" in fb and "test_new" in fb and "tests/test_a.py" in fb and "2b.1-noop" in fb


def test_wiring():
    import inspect
    from backend.core.ouroboros.governance import orchestrator as orch, lesson_memory as LM
    from backend.core.ouroboros.governance.phase_runners import generate_runner as gr
    assert "missing_declared_symbols" in inspect.getsource(gr)
    assert "symbols_missing_from_candidate" in inspect.getsource(orch.GovernedOrchestrator._run_validation)
    assert "_gr_exit_terminal" in inspect.getsource(orch.GovernedOrchestrator.run)
    assert LM.classify_error("declared_symbol_missing: test_new") == "declared_symbol_missing"
    assert "noop_refused_declared_symbols" in LM._MITIGATIONS
