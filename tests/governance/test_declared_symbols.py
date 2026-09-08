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


# --------------------------------------------------------------------------
# a declared symbol must CHANGE — a comment edit or the gate's punctuation
# rewrite is not a change (f97f8195d6, 2026-09-08)
# --------------------------------------------------------------------------

_ORIG = (
    "class Engine:\n"
    "    def route(self, files):\n"
    "        # DIAGNOSABILITY — five decline points\n"
    "        if not files:\n"
    "            return None\n"
    "        return files[0]\n"
)


def test_a_declared_symbol_left_semantically_unchanged_is_reported(monkeypatch):
    from backend.core.ouroboros.governance.declared_symbols import symbols_unchanged_in_candidate
    monkeypatch.setenv("JARVIS_DECLARED_SYMBOLS_ENABLED", "true")
    comment_only = _ORIG.replace("DIAGNOSABILITY — five", "DIAGNOSABILITIY - five")
    assert symbols_unchanged_in_candidate(("_route",), {"full_content": comment_only}, _ORIG) == ()
    assert symbols_unchanged_in_candidate(("route",), {"full_content": comment_only}, _ORIG) == ("route",)
    assert symbols_unchanged_in_candidate(("Engine.route",), {"full_content": comment_only}, _ORIG) == ("Engine.route",)
    changed = _ORIG.replace("        return files[0]\n", "        if len(files) != 1:\n            return None\n        return files[0]\n")
    assert symbols_unchanged_in_candidate(("route",), {"full_content": changed}, _ORIG) == ()
    # a new symbol is not "unchanged"; an unknown original judges nothing; no content judges nothing
    assert symbols_unchanged_in_candidate(("brand_new",), {"full_content": comment_only}, _ORIG) == ()
    assert symbols_unchanged_in_candidate(("route",), {"full_content": comment_only}, None) == ()
    assert symbols_unchanged_in_candidate(("route",), {"unified_diff": "@@"}, _ORIG) == ()


def test_validate_refuses_an_unchanged_declared_symbol_and_the_runner_gate_sees_the_original():
    import inspect
    from backend.core.ouroboros.governance import orchestrator
    from backend.core.ouroboros.governance.phase_runners import generate_runner
    src = inspect.getsource(orchestrator)
    assert "declared_symbol_unchanged: " in src
    assert src.index("declared_symbol_unchanged: ") < src.index("result = await self._run_validation_core(ctx, candidate, remaining_s)")
    assert "original=orch._original_text_for(_cand)" in inspect.getsource(generate_runner)
