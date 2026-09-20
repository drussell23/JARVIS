"""The micro-fix repairs the CANDIDATE, not whatever is on disk.

Across this repo's entire session history the micro-fix ran 532 times and
repaired nothing: 375 attempts reached ``repair()`` and every one returned
"disabled", and 157 never got that far because ``_repair_abs.is_file()``
refused a file the op existed to create. ``micro_fix_succeeded_break`` has
never appeared in a log.

Four causes, one family -- the loop was never given the execution context it
needs:

1. the master switch defaulted OFF, for the stated reason that the loop
   "writes to disk outside the Iron Gate ... until re-homed";
2. the caller gated on the target file EXISTING, so creation goals were
   skipped outright;
3. the caller read the content from DISK, which on a modification is the
   pre-edit original -- VALIDATE never applies a candidate to the operator
   tree -- so the loop reported on code nobody asked it to repair;
4. ``_run_and_capture`` accepted that content and ignored it, testing
   whatever happened to be on disk instead.

These tests pin the behavior, not the wiring: each one fails on a
regression that a source-text pin would wave through.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.interactive_repair import (
    InteractiveRepairLoop,
    repair_permitted,
)
from backend.core.ouroboros.governance.phase_runners.validate_runner import (
    _candidate_files,
    _micro_fix_budget_s,
    _repaired_candidate,
    _resolve_repair_plan,
)


def _passing_argv() -> list:
    """A command that always succeeds, so ``repair`` converges immediately.

    ``sys.executable`` rather than ``python3``: the daemon's PATH does not
    contain the venv, so a bare ``python3`` is the system interpreter, which
    has no pytest -- the defect this argv shape exists to avoid.
    """
    return [sys.executable, "-c", ""]


def _asserting_argv(target: str, expected: str) -> list:
    """Succeeds only if *target* on disk holds *expected*."""
    return [
        sys.executable, "-c",
        "import pathlib,sys;"
        f"sys.exit(0 if pathlib.Path({target!r}).read_text() == {expected!r} else 1)",
    ]


# ---------------------------------------------------------------------------
# Permission: isolation is a property of the call site
# ---------------------------------------------------------------------------


def test_unsandboxed_caller_stays_refused(monkeypatch):
    """Unset env + not isolated is still OFF -- the pre-fix default."""
    monkeypatch.delenv("JARVIS_INTERACTIVE_REPAIR_ENABLED", raising=False)
    assert repair_permitted(isolated=False) is False


def test_isolation_grants_permission(monkeypatch):
    """A caller that has built a throwaway root satisfies the condition the
    switch was guarding: the writes cannot reach the Iron Gate's territory."""
    monkeypatch.delenv("JARVIS_INTERACTIVE_REPAIR_ENABLED", raising=False)
    assert repair_permitted(isolated=True) is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " Off "])
def test_explicit_kill_switch_beats_isolation(monkeypatch, value):
    """An operator's explicit `false` must win everywhere, sandbox or not."""
    monkeypatch.setenv("JARVIS_INTERACTIVE_REPAIR_ENABLED", value)
    assert repair_permitted(isolated=True) is False
    assert repair_permitted(isolated=False) is False


def test_legacy_env_still_enables_unsandboxed(monkeypatch):
    """The old opt-in keeps working for any caller that still wants it."""
    monkeypatch.setenv("JARVIS_INTERACTIVE_REPAIR_ENABLED", "true")
    assert repair_permitted(isolated=False) is True


@pytest.mark.asyncio
async def test_refused_loop_echoes_its_input(monkeypatch, tmp_path):
    """A refusal must not silently report empty repaired content."""
    monkeypatch.delenv("JARVIS_INTERACTIVE_REPAIR_ENABLED", raising=False)
    loop = InteractiveRepairLoop(provider=None, project_root=tmp_path)
    result = await loop.repair(
        file_path="a.py", file_content="x = 1\n",
        test_argv=_passing_argv(), op_id="op-refused",
    )
    assert result.fixed is False
    assert result.repaired_content == "x = 1\n"
    assert not (tmp_path / "a.py").exists(), "a refused loop must not write"


# ---------------------------------------------------------------------------
# The creation case: 157 skips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repairs_a_file_that_does_not_exist_yet(tmp_path):
    """The whole point of a test-synthesis goal: the file is the output.

    The old caller gated on ``_repair_abs.is_file()`` and skipped. Nested
    directories are created too -- a candidate may propose
    ``tests/governance/test_new.py`` into a tree that has neither.
    """
    rel = "tests/governance/test_brand_new.py"
    content = "def test_ok():\n    assert True\n"
    loop = InteractiveRepairLoop(
        provider=None, project_root=tmp_path, isolated=True,
    )

    result = await loop.repair(
        file_path=rel, file_content=content,
        test_argv=_passing_argv(), op_id="op-create",
    )

    assert result.fixed is True
    assert result.repaired_content == content
    assert (tmp_path / rel).read_text() == content, (
        "the candidate was never materialized — the creation case is still "
        "unreachable."
    )


# ---------------------------------------------------------------------------
# The modification case: repairing the wrong text, silently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidate_content_overwrites_stale_disk_state(tmp_path):
    """Disk holds the pre-edit original; the loop must test the candidate.

    This is the failure nobody could see: the loop ran, reported, and was
    judging code the op had not proposed.
    """
    rel = "pkg/mod.py"
    (tmp_path / "pkg").mkdir()
    (tmp_path / rel).write_text("ORIGINAL\n")
    candidate = "CANDIDATE\n"

    loop = InteractiveRepairLoop(
        provider=None, project_root=tmp_path, isolated=True,
    )
    result = await loop.repair(
        file_path=rel, file_content=candidate,
        test_argv=_asserting_argv(str(tmp_path / rel), candidate),
        op_id="op-modify",
    )

    assert result.fixed is True, (
        "the subprocess did not see the candidate's text — _run_and_capture "
        "is ignoring the content it was handed again."
    )
    assert (tmp_path / rel).read_text() == candidate


# ---------------------------------------------------------------------------
# Candidate contract
# ---------------------------------------------------------------------------


def test_diff_candidates_need_no_special_handling():
    """A ``2b.1-diff`` candidate is normalized to ``full_content`` by the
    provider at parse time, so the resolver only ever sees resolved text."""
    assert _candidate_files(
        {"file_path": "a.py", "full_content": "patched\n",
         "unified_diff": "@@ -1 +1 @@\n-x\n+patched\n"},
    ) == (("a.py", "patched\n"),)


def test_raw_content_alias_accepted():
    assert _candidate_files(
        {"file_path": "a.py", "raw_content": "x = 1\n"},
    ) == (("a.py", "x = 1\n"),)


def test_multi_file_candidate_yields_every_file():
    """All of them are materialized: a sibling left behind makes the
    candidate fail for reasons unrelated to the code under repair."""
    plan = _resolve_repair_plan(
        candidates=[{"files": [
            {"file_path": "a.py", "full_content": "x = 1\n"},
            {"file_path": "b.py", "full_content": "y = 2\n"},
        ]}],
        target_files=(),
    )
    assert plan is not None
    assert [p for p, _ in plan[1]] == ["a.py", "b.py"]


def test_candidate_files_never_raises_on_junk():
    for junk in (None, "", 42, [], {"files": "not-a-list"}, {"files": [None, 7]}):
        assert _candidate_files(junk) == ()


def test_resolver_skips_contentless_candidate_for_one_that_has_content():
    """A candidate proposing nothing must not shadow one that does."""
    plan = _resolve_repair_plan(
        candidates=[{"file_path": "empty.py"},
                    {"file_path": "real.py", "full_content": "x = 1\n"}],
        target_files=(),
    )
    assert plan is not None
    assert plan[0] == "real.py"


# ---------------------------------------------------------------------------
# Carrying the repair forward
# ---------------------------------------------------------------------------


def test_repaired_candidate_preserves_provenance():
    """``source_hash`` must survive: the post-loop drift check reads it, and
    it has to remain the hash taken at GENERATE time."""
    original = {
        "candidate_id": "c1", "file_path": "a.py",
        "full_content": "broken\n", "source_hash": "abc123",
        "source_path": "a.py",
    }
    out = _repaired_candidate(
        candidates=[original], file_path="a.py", content="fixed\n",
    )
    assert out is not None
    assert out["full_content"] == "fixed\n"
    assert out["source_hash"] == "abc123"
    assert out["candidate_id"] == "c1"
    assert original["full_content"] == "broken\n", "original was mutated"


def test_repaired_candidate_drops_superseded_diff():
    """A stale ``unified_diff`` alongside repaired text would let a consumer
    re-derive the pre-repair content."""
    out = _repaired_candidate(
        candidates=[{"file_path": "a.py", "full_content": "broken\n",
                     "unified_diff": "@@ -1 +1 @@\n-x\n+broken\n"}],
        file_path="a.py", content="fixed\n",
    )
    assert out is not None
    assert "unified_diff" not in out


def test_repaired_candidate_updates_the_matching_nested_file_only():
    out = _repaired_candidate(
        candidates=[{"files": [
            {"file_path": "a.py", "full_content": "keep\n"},
            {"file_path": "b.py", "full_content": "broken\n"},
        ]}],
        file_path="b.py", content="fixed\n",
    )
    assert out is not None
    assert {e["file_path"]: e["full_content"] for e in out["files"]} == {
        "a.py": "keep\n", "b.py": "fixed\n",
    }


def test_no_repaired_candidate_without_content():
    assert _repaired_candidate(
        candidates=[{"file_path": "a.py", "full_content": "x\n"}],
        file_path="a.py", content="",
    ) is None


# ---------------------------------------------------------------------------
# Budget is derived, not declared
# ---------------------------------------------------------------------------


class _Cfg:
    validation_timeout_s = 600.0


class _Orch:
    _config = _Cfg()


class _Ctx:
    pipeline_deadline = None


def test_budget_tracks_the_loop_shape(monkeypatch):
    """The old call site spent a flat 90s regardless of configuration."""
    monkeypatch.setenv("JARVIS_INTERACTIVE_REPAIR_MAX_ITERS", "2")
    monkeypatch.setenv("JARVIS_INTERACTIVE_REPAIR_TIMEOUT_S", "10")
    small = _micro_fix_budget_s(_Ctx(), _Orch())
    monkeypatch.setenv("JARVIS_INTERACTIVE_REPAIR_MAX_ITERS", "8")
    large = _micro_fix_budget_s(_Ctx(), _Orch())
    assert large > small, "budget does not track the loop's configured shape"


def test_budget_never_claims_more_than_half_the_op_clock(monkeypatch):
    """The retry that follows, and L2 after it, share the same envelope."""
    monkeypatch.setenv("JARVIS_INTERACTIVE_REPAIR_MAX_ITERS", "50")
    monkeypatch.setenv("JARVIS_INTERACTIVE_REPAIR_TIMEOUT_S", "60")

    class _Tight:
        validation_timeout_s = 40.0

    class _TightOrch:
        _config = _Tight()

    assert _micro_fix_budget_s(_Ctx(), _TightOrch()) == pytest.approx(20.0)


def test_budget_is_positive_even_with_no_clock():
    class _Zero:
        validation_timeout_s = 0.0

    class _ZeroOrch:
        _config = _Zero()

    assert _micro_fix_budget_s(_Ctx(), _ZeroOrch()) > 0.0
