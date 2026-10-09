"""Land the passing majority of an authored test file (2026-10-09).

One refuted expectation among nine real tests discarded all nine at VALIDATE.
``test_reality.prune_failing_tests`` removes exactly the tests the subject
refuted -- refusing every case where that could hide a real red -- and the
orchestrator re-validates the pruned copy from scratch before adopting it.
"""
from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

from backend.core.ouroboros.governance import test_reality as tr
from backend.core.ouroboros.governance.op_context import ValidationResult

SUBJECT = "backend.autonomy.widget"

SRC = '''\
import pytest
from backend.autonomy.widget import make


def test_ok():
    assert make(1) == 1


@pytest.mark.parametrize("n", [1, 2])
def test_wrong(n):
    assert make(n) == -n


class TestGroup:
    def test_keep(self):
        assert make(2) == 2

    def test_bad(self):
        assert make(3) == 0
'''

PATH = "tests/autonomy/test_widget.py"


def _prune(ids, *, files=None, protected=(), root=None, subject=SUBJECT):
    return tr.prune_failing_tests(
        files or [(PATH, SRC)], ids, repo_root=root or "/nonexistent-root",
        subject=subject, protected=protected,
    )


def test_parametrized_ids_remove_the_whole_function():
    out = _prune([f"{PATH}::test_wrong[1]", f"{PATH}::test_wrong[2]"])
    assert out is not None
    contents, pruned = out
    body = contents[PATH]
    assert "def test_wrong" not in body and "parametrize" not in body
    assert "def test_ok" in body and "def test_keep" in body
    assert pruned == (f"{PATH}::test_wrong",)


def test_class_methods_are_removed_and_the_class_kept():
    contents, pruned = _prune([f"{PATH}::TestGroup::test_bad"])
    body = contents[PATH]
    assert "test_bad" not in body and "class TestGroup" in body and "test_keep" in body
    assert pruned == (f"{PATH}::TestGroup::test_bad",)
    compile(body, PATH, "exec")


def test_a_class_emptied_of_tests_goes_with_them():
    src = SRC.replace("    def test_keep(self):\n        assert make(2) == 2\n\n", "")
    contents, _ = _prune([f"{PATH}::TestGroup::test_bad"], files=[(PATH, src)])
    assert "class TestGroup" not in contents[PATH]


def test_sandbox_prefixed_ids_still_place_their_file():
    assert _prune([f"/tmp/sbx/abc/{PATH}::test_ok"]) is not None


def test_refuses_when_nothing_would_remain():
    ids = [f"{PATH}::test_ok", f"{PATH}::test_wrong[1]",
           f"{PATH}::TestGroup::test_keep", f"{PATH}::TestGroup::test_bad"]
    assert _prune(ids) is None


def test_refuses_when_the_remainder_drops_the_subject():
    src = SRC.replace("assert make(1) == 1", "assert 1 == 1").replace(
        "assert make(2) == 2", "assert 2 == 2")
    # only test_wrong and test_bad touch the subject; pruning them leaves none that do
    assert _prune([f"{PATH}::test_wrong[1]", f"{PATH}::TestGroup::test_bad"],
                  files=[(PATH, src)]) is None


def test_refuses_a_candidate_that_also_changes_production_code():
    files = [("backend/autonomy/widget.py", "def make(n):\n    return n\n"), (PATH, SRC)]
    assert _prune([f"{PATH}::TestGroup::test_bad"], files=files) is None


def test_refuses_an_acceptance_test_and_an_unplaceable_id():
    assert _prune([f"{PATH}::TestGroup::test_bad"], protected={"test_bad"}) is None
    assert _prune(["tests/other/test_x.py::test_bad"]) is None
    assert _prune([f"{PATH}::test_missing"]) is None


def test_refuses_without_a_subject():
    assert _prune([f"{PATH}::TestGroup::test_bad"], subject=None) is None


def test_refuses_to_delete_a_test_already_on_disk(tmp_path):
    on_disk = tmp_path / PATH
    on_disk.parent.mkdir(parents=True)
    on_disk.write_text("def test_bad():\n    assert True\n")
    src = SRC.replace("class TestGroup:\n    def test_keep(self):", "def test_bad():\n    assert 0\n\n\nclass TestGroup:\n    def test_keep(self):")
    assert _prune([f"{PATH}::test_bad"], files=[(PATH, src)], root=tmp_path) is None


# --------------------------------------------------------------------------- seam


def _orch(core_results, tmp_path):
    from backend.core.ouroboros.governance import orchestrator as om

    o = om.Orchestrator.__new__(om.Orchestrator)
    o._config = SimpleNamespace(project_root=tmp_path)
    seen = []

    async def core(ctx, cand, remaining):
        seen.append(dict(cand))
        return core_results.pop(0)(cand)
    o._run_validation_core = core
    return o, seen


def _failed(ids, total):
    return ValidationResult(
        passed=False, best_candidate=None, validation_duration_s=1.0,
        error="validation failed: test", failure_class="test",
        failed_node_ids=tuple(ids), test_failed=len(ids), test_total=total,
    )


def _passed(cand):
    return ValidationResult(passed=True, best_candidate=cand, validation_duration_s=1.0, error=None)


def _ctx():
    return SimpleNamespace(op_id="op-x", description="write tests for widget",
                           target_files=(PATH,), target_symbols=())


def test_seam_adopts_a_pruned_candidate_only_after_it_re_validates(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_op_subject", lambda *a, **k: SUBJECT)
    first = _failed([f"{PATH}::TestGroup::test_bad"], 5)
    o, seen = _orch([_passed], tmp_path)
    cand = {"file_path": PATH, "full_content": SRC}
    out = asyncio.run(o._land_passing_majority(_ctx(), cand, first, 100.0))
    assert out.passed and out.pruned_tests == (f"{PATH}::TestGroup::test_bad",)
    assert out.best_candidate is cand                       # APPLY applies THIS dict
    assert "test_bad" not in cand["full_content"]
    assert cand["test_prune"]["original"][PATH] == SRC
    assert "test_bad" not in seen[0]["full_content"]         # the re-run judged the pruned file


def test_seam_keeps_the_original_verdict_when_re_validation_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_op_subject", lambda *a, **k: SUBJECT)
    first = _failed([f"{PATH}::TestGroup::test_bad"], 5)
    o, _ = _orch([lambda c: _failed([f"{PATH}::test_ok"], 4)], tmp_path)
    cand = {"file_path": PATH, "full_content": SRC}
    out = asyncio.run(o._land_passing_majority(_ctx(), cand, first, 100.0))
    assert out is first and cand["full_content"] == SRC
    # once per candidate: a second pass does not prune again
    o2, seen2 = _orch([_passed], tmp_path)
    assert asyncio.run(o2._land_passing_majority(_ctx(), cand, first, 100.0)) is first
    assert seen2 == []


def test_seam_refuses_a_cut_off_run_and_a_capped_id_list(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_op_subject", lambda *a, **k: SUBJECT)
    o, seen = _orch([_passed, _passed], tmp_path)
    timed = dataclasses.replace(_failed([f"{PATH}::TestGroup::test_bad"], 5), timed_out=True)
    capped = dataclasses.replace(_failed([f"{PATH}::TestGroup::test_bad"], 5), test_failed=2)
    for res in (timed, capped):
        cand = {"file_path": PATH, "full_content": SRC}
        assert asyncio.run(o._land_passing_majority(_ctx(), cand, res, 100.0)) is res
    assert seen == []


def test_seam_rewrites_multi_file_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_op_subject", lambda *a, **k: SUBJECT)
    first = _failed([f"{PATH}::TestGroup::test_bad"], 5)
    o, _ = _orch([_passed], tmp_path)
    cand = {"file_path": PATH, "full_content": SRC,
            "files": [{"file_path": PATH, "full_content": SRC}]}
    out = asyncio.run(o._land_passing_majority(_ctx(), cand, first, 100.0))
    assert out.passed and "test_bad" not in cand["files"][0]["full_content"]


def test_the_validation_seam_calls_it():
    import inspect
    from backend.core.ouroboros.governance import orchestrator as om
    assert "self._land_passing_majority(" in inspect.getsource(om.Orchestrator._run_validation)
