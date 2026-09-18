"""A diff has nothing to anchor to when the op's job is to create the file.

Measured live in `bt-2026-09-18-033008`: every test-synthesis goal died
`_schema_invalid:diff_source_unreadable`. The served 30B resolved to
`full_content_and_diff`, so the lean builder emitted the diff schema — for a
file the goal existed to WRITE. The reply could not validate against a source
that was not there, local-primary fell through the cascade, and the op went
terminal with the goal unsatisfied. Fifteen of twenty-seven queued goals are
that shape, so the largest liveness tier could never land.

The message said "unreadable". The state was "absent". This pins that
distinction, because collapsing it in either direction has now cost a soak
once in each direction:

* collapsing absent INTO unreadable is this defect — a creation asked for a
  diff;
* collapsing unreadable INTO absent was the previous one — a transient read
  failure pushing small-file edits into whole-file re-emission, three damaged
  autonomous commits.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import providers as P


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "exists.py").write_text("x = 1\n")
    return tmp_path


# --------------------------------------------------------------------------
# target_is_creation — three values, and the third is the point
# --------------------------------------------------------------------------

def test_an_absent_target_is_a_creation(tree: Path):
    assert P.target_is_creation(["tests/test_new.py"], tree) is True


def test_an_existing_target_is_an_edit(tree: Path):
    assert P.target_is_creation(["backend/exists.py"], tree) is False


def test_an_unknowable_root_answers_NEITHER(tree: Path):
    """`None`, not `False`. Without a root, absence cannot be established, and
    an unknown root must never flip the schema in either direction."""
    assert P.target_is_creation(["tests/test_new.py"], None) is None
    assert P.target_is_creation([], tree) is None
    assert P.target_is_creation(None, tree) is None


def test_it_never_reads_the_file(tree: Path):
    """The previous defect was a READ failing soft into a whole-file rewrite.
    There must be no read left to fail — `exists()` is a stat, not a read."""
    import inspect

    body = inspect.getsource(P.target_is_creation).split('"""')[-1]
    assert "read_text" not in body
    assert "open(" not in body


def test_an_absolute_target_resolves_without_the_root(tree: Path):
    assert P.target_is_creation([str(tree / "backend" / "exists.py")], tree) is False
    assert P.target_is_creation([str(tree / "nope.py")], tree) is True


def test_any_absent_target_makes_the_op_a_creation(tree: Path):
    """A diff is unsatisfiable for the absent file, and the op is judged whole."""
    assert P.target_is_creation(
        ["backend/exists.py", "tests/test_new.py"], tree,
    ) is True


def test_it_never_raises(tree: Path):
    for bad in ([object()], ["\x00bad"], [""]):
        assert P.target_is_creation(bad, tree) in (True, False, None)


# --------------------------------------------------------------------------
# The gate — capability answers "can the model", this answers "can this op"
# --------------------------------------------------------------------------

def test_a_creation_forces_full_content_even_for_a_diff_capable_model(tree: Path):
    """THE regression. The capability was resolved correctly and the op was
    still unsatisfiable, because no capability of the model can produce a diff
    against a file that is not there."""
    assert P.resolve_force_full_content(
        schema_capability="full_content_and_diff",
        target_files=["tests/test_new.py"], repo_root=tree,
    ) is True


def test_an_edit_by_a_diff_capable_model_still_gets_the_diff(tree: Path):
    """The previous fix must survive this one: a 144-line file takes the diff
    path, and three autonomous commits' worth of re-emission drift stays gone."""
    assert P.resolve_force_full_content(
        schema_capability="full_content_and_diff",
        target_files=["backend/exists.py"], repo_root=tree,
    ) is False


def test_a_weak_model_still_gets_full_content_either_way(tree: Path):
    for target in ("backend/exists.py", "tests/test_new.py"):
        assert P.resolve_force_full_content(
            schema_capability="full_content_only",
            target_files=[target], repo_root=tree,
        ) is True


def test_an_unreadable_target_STILL_cannot_change_the_schema():
    """The pin from the previous fix, unchanged. `repo_root=None` means absence
    was never established, so capability alone decides — in both directions."""
    assert P.resolve_force_full_content(
        schema_capability="full_content_and_diff",
        target_files=["x.py"], repo_root=None,
    ) is False
    assert P.resolve_force_full_content(
        schema_capability="full_content_only",
        target_files=["x.py"], repo_root=None,
    ) is True


def test_no_size_heuristic_came_back_with_it(tree: Path):
    """The cost heuristic must not return through this door.

    Scoped to `target_is_creation`: `resolve_force_full_content` still passes
    `threshold_lines=0` through to the pure capability predicate, which is the
    existing call and carries no authority. Asserting on that too would fail on
    the argument name alone and pin nothing.
    """
    import inspect

    body = inspect.getsource(P.target_is_creation).split('"""')[-1]
    for banned in ("threshold_lines", "line_count", "read_text"):
        assert banned not in body


# --------------------------------------------------------------------------
# The coupling — the fix must not become the next defect
# --------------------------------------------------------------------------

def test_a_creation_is_not_reported_as_capability_drift(tree: Path, monkeypatch):
    """Without this branch the fix is strictly worse than the defect.

    `CapabilityExecutionDrift` is FATAL because, with the size gate gone, a
    diff-capable single-file op coming out full_content had no legitimate cause
    left. Making creations take full_content creates one — so every
    test-synthesis op would arrive at that verdict and be refused as a silent
    downgrade, leaving the largest tier exactly as unlandable as before, with a
    different error.
    """
    import types

    from backend.core.ouroboros.governance import capability_assurance as CA

    monkeypatch.chdir(tree)
    monkeypatch.setenv("JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED", "true")
    ctx = types.SimpleNamespace(
        target_files=("tests/test_new.py",),
        repo_root=tree,
        telemetry=types.SimpleNamespace(
            routing_intent=types.SimpleNamespace(
                served_model="qwen3-coder-ov:30b",
                schema_capability="full_content_and_diff",
            ),
        ),
    )
    verdict = CA.assert_generation_capability(ctx, force_full_content=True)
    assert verdict.ok is True
    assert "creation op" in verdict.reason
    assert "CapabilityExecutionDrift" not in verdict.reason


def test_an_EDIT_that_comes_out_full_content_is_still_drift(tree: Path, monkeypatch):
    """The guard must keep its teeth. Only the creation case is excused."""
    import types

    from backend.core.ouroboros.governance import capability_assurance as CA

    monkeypatch.chdir(tree)
    monkeypatch.setenv("JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED", "true")
    ctx = types.SimpleNamespace(
        target_files=("backend/exists.py",),
        repo_root=tree,
        telemetry=types.SimpleNamespace(
            routing_intent=types.SimpleNamespace(
                served_model="qwen3-coder-ov:30b",
                schema_capability="full_content_and_diff",
            ),
        ),
    )
    verdict = CA.assert_generation_capability(ctx, force_full_content=True)
    assert verdict.ok is False
    assert "CapabilityExecutionDrift" in verdict.reason


def test_the_verdict_asks_the_same_seam_the_schema_decision_used(tree: Path):
    """A second existence check here could disagree with the one that actually
    chose the schema — and catching that disagreement is what this verdict is
    FOR, so it must not be the thing introducing one."""
    import inspect

    from backend.core.ouroboros.governance import capability_assurance as CA

    src = inspect.getsource(CA.assert_generation_capability)
    assert "target_is_creation" in src
    assert "exists()" not in src   # not re-derived locally
