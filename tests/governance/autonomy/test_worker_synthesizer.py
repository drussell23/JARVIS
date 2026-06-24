"""Tests for worker_synthesizer — THE Golden Rule (no static role table)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Tuple

import pytest

from backend.core.ouroboros.governance.autonomy.worker_synthesizer import (
    WorkerShape,
    _MUTATION_TOOLS,
    render_worker_system_prompt,
    synthesize_worker_spec,
)


@dataclass
class _SubGoal:
    goal: str
    target_files: Tuple[str, ...]


# ---------------------------------------------------------------------------
# fixtures — real files on disk for AST inspection
# ---------------------------------------------------------------------------


@pytest.fixture
def py_source(tmp_path):
    p = tmp_path / "module_under_work.py"
    p.write_text(
        "import os\n\n\n"
        "def alpha(x):\n    return x + 1\n\n\n"
        "class Beta:\n    def m(self):\n        return 2\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def test_file(tmp_path):
    p = tmp_path / "test_something.py"
    p.write_text(
        "def test_truth():\n    assert True\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def docs_file(tmp_path):
    p = tmp_path / "GUIDE.md"
    p.write_text("# Guide\n\nSome prose.\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Golden Rule: read/analyze -> read-only worker, NO mutation tools
# ---------------------------------------------------------------------------


def test_read_goal_synthesizes_read_only_no_mutation_tools(py_source, tmp_path):
    sg = _SubGoal(
        goal="Analyze the alpha function and report its callers",
        target_files=(py_source.name,),
    )
    shape = synthesize_worker_spec(sg, project_root=str(tmp_path))
    assert shape.read_only is True
    assert shape.mutation_budget == 0
    assert not shape.is_mutating
    # No mutation tool may appear.
    assert not any(t in _MUTATION_TOOLS for t in shape.allowed_tools)
    # Read base tools are present.
    assert "read_file" in shape.allowed_tools
    assert "search_code" in shape.allowed_tools


# ---------------------------------------------------------------------------
# Golden Rule: mutate goal (AST shows a writable source) -> bounded mutation tool
# ---------------------------------------------------------------------------


def test_mutate_goal_synthesizes_bounded_mutation_tool(py_source, tmp_path):
    sg = _SubGoal(
        goal="Implement and fix the alpha function to add input validation",
        target_files=(py_source.name,),
    )
    shape = synthesize_worker_spec(sg, project_root=str(tmp_path))
    assert shape.read_only is False
    assert shape.mutation_budget > 0
    assert shape.is_mutating
    assert any(t in _MUTATION_TOOLS for t in shape.allowed_tools)
    # The mutation surface is bounded, not unbounded.
    assert shape.mutation_budget == int(
        os.environ.get("JARVIS_SWARM_DEFAULT_MUTATION_BUDGET", "3")
    )


# ---------------------------------------------------------------------------
# Golden Rule: two different sub-goals -> DIFFERENT shapes derived from
# goal/target_files (NOT from a lookup table)
# ---------------------------------------------------------------------------


def test_two_sub_goals_derive_different_shapes(py_source, test_file, docs_file, tmp_path):
    root = str(tmp_path)
    # A test-touching mutate goal -> gets run_tests + edit.
    g_test = _SubGoal(
        goal="Add a regression test asserting alpha rejects negatives",
        target_files=(test_file.name,),
    )
    # A docs analyze goal -> no edit on a .py, no run_tests.
    g_docs = _SubGoal(
        goal="Summarize the guide document for the changelog",
        target_files=(docs_file.name,),
    )
    s_test = synthesize_worker_spec(g_test, project_root=root)
    s_docs = synthesize_worker_spec(g_docs, project_root=root)

    # Shapes differ — derived, not table-keyed.
    assert s_test.allowed_tools != s_docs.allowed_tools
    assert s_test.role != s_docs.role

    # The test-touching goal got run_tests; the docs goal did NOT.
    assert "run_tests" in s_test.allowed_tools
    assert "run_tests" not in s_docs.allowed_tools

    # Docs analyze goal is read-only -> no edit tools at all.
    assert not any(t in _MUTATION_TOOLS for t in s_docs.allowed_tools)
    # Role labels are composed from inspected material, not enum members.
    assert "test-suite" in s_test.role
    assert "docs" in s_docs.role


def test_role_is_freeform_composed_not_enum(py_source, tmp_path):
    sg = _SubGoal(
        goal="Inspect alpha",
        target_files=(py_source.name,),
    )
    shape = synthesize_worker_spec(sg, project_root=str(tmp_path))
    # Composed string of "<material> <action>" — derived from the python
    # source + read intent, not a fixed identifier.
    assert "python-source" in shape.role
    assert "analyzer" in shape.role


def test_callgraph_tool_only_when_python_defines_symbols(py_source, docs_file, tmp_path):
    root = str(tmp_path)
    s_py = synthesize_worker_spec(
        _SubGoal(goal="Analyze alpha", target_files=(py_source.name,)),
        project_root=root,
    )
    s_docs = synthesize_worker_spec(
        _SubGoal(goal="Read the guide", target_files=(docs_file.name,)),
        project_root=root,
    )
    assert "get_callers" in s_py.allowed_tools
    assert "get_callers" not in s_docs.allowed_tools


# ---------------------------------------------------------------------------
# Fail-CLOSED: ambiguous / unparseable / missing -> read-only fallback
# ---------------------------------------------------------------------------


def test_ambiguous_goal_falls_back_read_only(py_source, tmp_path):
    sg = _SubGoal(goal="alpha thing stuff", target_files=(py_source.name,))
    shape = synthesize_worker_spec(sg, project_root=str(tmp_path))
    assert shape.read_only is True
    assert not any(t in _MUTATION_TOOLS for t in shape.allowed_tools)


def test_unparseable_target_blocks_mutation(tmp_path):
    bad = tmp_path / "broken.py"
    bad.write_text("def (((:\n  not python\n", encoding="utf-8")
    sg = _SubGoal(
        goal="Refactor and rewrite the broken module",
        target_files=(bad.name,),
    )
    shape = synthesize_worker_spec(sg, project_root=str(tmp_path))
    # Goal verbs say mutate, but the target is unparseable -> fail-CLOSED.
    assert shape.read_only is True
    assert not any(t in _MUTATION_TOOLS for t in shape.allowed_tools)


def test_missing_target_with_create_goal_allows_write(tmp_path):
    sg = _SubGoal(
        goal="Create a new helper module implementing a parser",
        target_files=("new_helper.py",),
    )
    shape = synthesize_worker_spec(sg, project_root=str(tmp_path))
    # A create (file does not exist) is a legitimate mutation.
    assert shape.read_only is False
    assert "write_file" in shape.allowed_tools or "edit_file" in shape.allowed_tools


def test_empty_subgoal_is_read_only(tmp_path):
    sg = _SubGoal(goal="", target_files=())
    shape = synthesize_worker_spec(sg, project_root=str(tmp_path))
    assert shape.read_only is True
    assert shape.mutation_budget == 0
    assert not any(t in _MUTATION_TOOLS for t in shape.allowed_tools)


def test_synthesizer_never_raises_on_garbage():
    class _Garbage:
        goal = None
        target_files = None

    shape = synthesize_worker_spec(_Garbage())
    assert isinstance(shape, WorkerShape)
    assert shape.read_only is True


# ---------------------------------------------------------------------------
# Context budget proportional to inspected scope, clamped
# ---------------------------------------------------------------------------


def test_context_budget_within_env_bounds(py_source, tmp_path):
    shape = synthesize_worker_spec(
        _SubGoal(goal="Analyze alpha", target_files=(py_source.name,)),
        project_root=str(tmp_path),
    )
    lo = int(os.environ.get("JARVIS_SWARM_MIN_CONTEXT_TOKENS", "4000"))
    hi = int(os.environ.get("JARVIS_SWARM_MAX_CONTEXT_TOKENS", "64000"))
    assert lo <= shape.context_budget_tokens <= hi


# ---------------------------------------------------------------------------
# render_worker_system_prompt — generalized renderer
# ---------------------------------------------------------------------------


def test_render_worker_prompt_includes_synthesized_fields():
    prompt = render_worker_system_prompt(
        role="python-source mutator",
        goal="Fix the bug",
        scope_paths=["a.py", "b.py"],
        allowed_tools=["read_file", "edit_file"],
        mutation_budget=3,
        read_only=False,
    )
    assert "python-source mutator" in prompt
    assert "Fix the bug" in prompt
    assert "a.py" in prompt and "b.py" in prompt
    assert "edit_file" in prompt
    assert "max_mutations = 3" in prompt
    assert "read_only_mode = FALSE" in prompt


def test_render_worker_prompt_read_only_mode():
    prompt = render_worker_system_prompt(
        role="docs analyzer",
        goal="Read it",
        scope_paths=["x.md"],
        allowed_tools=["read_file"],
        mutation_budget=0,
        read_only=True,
    )
    assert "read_only_mode = TRUE" in prompt
    assert "max_mutations = 0" in prompt
