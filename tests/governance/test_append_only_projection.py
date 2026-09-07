"""Append-only projection: an "add tests" candidate keeps existing code intact.

Root cause covered: goal-003's full-file rewrite passed VALIDATE and was
refused at APPLY by the semantic guardian (test_assertion_weakened) because
the model had "tidied" an existing assertion while appending its new test.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import append_only_projection as P

OLD = textwrap.dedent('''
    from __future__ import annotations
    from pkg.mod import build

    def test_a():
        sys, user = build("x")
        assert "```" not in user and "```" not in sys

    def test_b():
        assert build("y")
''').lstrip()


def test_pure_append_passes_through_unchanged():
    new = OLD + "\ndef test_new():\n    assert 1 == 1\n"
    proj = P.project_append_only(OLD, new)
    assert not proj.changed and proj.content == new and proj.added == ("test_new",) and proj.dropped == ()


def test_edited_existing_assertion_is_discarded_and_new_test_kept():
    new = OLD.replace('"```" not in user and "```" not in sys', '"`" not in user and "`" not in sys') + "\ndef test_new():\n    assert 1 == 1\n"
    proj = P.project_append_only(OLD, new)
    assert proj.changed and proj.dropped == ("test_a",) and proj.added == ("test_new",)
    assert '"```" not in user' in proj.content          # pristine line restored
    assert "def test_new" in proj.content
    assert proj.content.startswith(OLD.rstrip("\n"))


def test_removed_existing_test_is_restored():
    new = OLD.replace("def test_b():\n    assert build(\"y\")\n", "") + "\ndef test_new():\n    assert True\n"
    proj = P.project_append_only(OLD, new)
    assert proj.changed and "def test_b" in proj.content and "def test_new" in proj.content


def test_new_imports_are_inserted_after_existing_imports():
    new = OLD.replace("from pkg.mod import build\n", "from pkg.mod import build\nimport pytest\n") \
             .replace('"```" not in user', '"`" not in user') + "\n@pytest.mark.x\ndef test_new():\n    assert True\n"
    proj = P.project_append_only(OLD, new)
    assert proj.changed
    lines = proj.content.splitlines()
    assert lines.index("import pytest") == lines.index("from pkg.mod import build") + 1
    assert "@pytest.mark.x" in proj.content and "def test_new" in proj.content
    import ast; ast.parse(proj.content)


def test_nothing_new_leaves_candidate_for_the_guardian():
    new = OLD.replace('"```" not in user', '"`" not in user')
    proj = P.project_append_only(OLD, new)
    assert not proj.changed and proj.content == new and proj.dropped == ("test_a",)


def test_syntax_error_passes_through():
    proj = P.project_append_only(OLD, "def broken(:\n")
    assert not proj.changed and proj.content == "def broken(:\n"


def test_project_candidates_only_for_existing_test_files(tmp_path, monkeypatch):
    monkeypatch.delenv(P._ENV_ENABLED, raising=False)
    tdir = tmp_path / "tests"; tdir.mkdir()
    (tdir / "test_x.py").write_text(OLD)
    edited = OLD.replace('"```" not in user', '"`" not in user') + "\ndef test_new():\n    assert True\n"
    cands = [
        {"candidate_id": "c1", "files": [
            {"file_path": "tests/test_x.py", "full_content": edited},
            {"file_path": "tests/test_brand_new.py", "full_content": "def test_z():\n    assert True\n"},
        ]},
        {"candidate_id": "c2", "file_path": "pkg/mod.py", "full_content": "def build(x):\n    return x\n"},
    ]
    rep = P.project_candidates(cands, tmp_path)
    assert rep.changed and len(rep.notes) == 1 and "test_x.py" in rep.notes[0]
    projected = rep.candidates[0]["files"][0]["full_content"]
    assert '"```" not in user' in projected and "def test_new" in projected
    assert rep.candidates[0]["files"][1]["full_content"].startswith("def test_z")   # new file untouched
    assert rep.candidates[1] is cands[1]                                              # production code never projected
    assert cands[0]["files"][0]["full_content"] == edited                              # input not mutated
    monkeypatch.setenv(P._ENV_ENABLED, "false")
    assert P.project_candidates(cands, tmp_path).changed is False


def test_projected_candidate_clears_the_guardian():
    from backend.core.ouroboros.governance.semantic_guardian import SemanticGuardian
    new = OLD.replace('"```" not in user and "```" not in sys', '"`" not in user and "`" not in sys') + "\ndef test_new():\n    assert True\n"
    before = SemanticGuardian().inspect(file_path="tests/test_x.py", old_content=OLD, new_content=new)
    assert any(getattr(f, "severity", "") == "hard" for f in before)
    proj = P.project_append_only(OLD, new)
    after = SemanticGuardian().inspect(file_path="tests/test_x.py", old_content=OLD, new_content=proj.content)
    assert not any(getattr(f, "severity", "") == "hard" for f in after)


def test_wiring():
    import inspect
    from backend.core.ouroboros.governance import orchestrator as orch, change_engine as ce, lesson_memory as LM
    assert "project_candidates" in inspect.getsource(orch)
    assert "Name the findings" in inspect.getsource(ce)
    assert "guardian_hard_finding" in LM._MITIGATIONS


def test_in_place_projection_mutates_the_shared_candidate(tmp_path):
    tdir = tmp_path / "tests"; tdir.mkdir()
    (tdir / "test_x.py").write_text(OLD)
    edited = OLD.replace('"```" not in user', '"`" not in user') + "\ndef test_new():\n    assert True\n"
    cand = {"candidate_id": "c1", "files": [{"file_path": "tests/test_x.py", "full_content": edited}]}
    entry = cand["files"][0]
    notes = P.project_candidate_in_place(cand, tmp_path)
    assert notes and "test_x.py" in notes[0]
    assert cand["files"][0] is entry                       # same dict objects — APPLY sees it
    assert '"```" not in user' in entry["full_content"] and "def test_new" in entry["full_content"]
    single = {"file_path": "tests/test_x.py", "full_content": edited}
    assert P.project_candidate_in_place(single, tmp_path) and '"```" not in user' in single["full_content"]
    clean = {"file_path": "tests/test_x.py", "full_content": OLD + "\ndef test_new():\n    assert True\n"}
    assert P.project_candidate_in_place(clean, tmp_path) == ()


def test_validation_seam_is_wired():
    import inspect
    from backend.core.ouroboros.governance import orchestrator as orch
    assert "project_candidate_in_place" in inspect.getsource(orch.GovernedOrchestrator._run_validation)
