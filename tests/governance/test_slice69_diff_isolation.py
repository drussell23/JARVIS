"""Slice 69 — Sovereign Worktree Alignment & Manifest Diff Isolation.

Two invariants, one cohesive slice (folds the held Slice 68 working-tree
capture in as substrate):

  Phase 1 (regression pin) — Slice 64's benchmark write-root routing is LIVE
  and must stay live. A ``ChangeRequest`` carrying a benchmark-sourced
  ``write_root`` (the prepared per-problem worktree) rebases its mutations
  INTO that worktree, never the JARVIS auto-commit workspace. We pin the
  existing behavior; we do NOT add swe_bench source-channel knowledge into the
  generic ``_redirect_target`` layer.

  Phase 2 (clean-delta invariant) — ``capture_produced_patch`` captures the
  WORKING-TREE diff vs ``base_commit`` (Slice 68 substrate: APPLY writes the
  candidate uncommitted), then STRIPS the harness's pre-applied ``test_patch``
  footprint (``prepared.target_paths``). The container scorer runs
  ``git apply <test_patch> && git apply <model_patch>`` — if the captured model
  patch still carries the test hunks, step 2 double-applies them and HARD-FAILS
  (no ``|| true`` guard), poisoning the score with ``scoring_error``. The final
  ``captured_patch`` must therefore contain 100% pure model source changes.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from backend.core.ouroboros.governance.change_engine import ChangeEngine
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (
    PreparedProblem,
    capture_produced_patch,
    DiffCaptureOutcome,
    _strip_test_patch_sections,
)


# ===========================================================================
# Phase 1 — Slice 64 benchmark write-root regression pin
# ===========================================================================


def test_benchmark_write_root_routes_mutation_into_prepared_worktree(tmp_path):
    """A benchmark-sourced write_root (prepared worktree) rebases the APPLY
    target INTO that worktree — the operator's main tree is never touched.

    Pins Slice 64 behavior WITHOUT touching _redirect_target: the generic
    layer takes a clean per-request write_root; the orchestrator owns the
    swe_bench_pro -> worktree mapping (no source-channel sniffing here)."""
    project_root = tmp_path / "repo"
    project_root.mkdir()
    worktree = tmp_path / "prepared_worktree"
    worktree.mkdir()
    # _redirect_target / _effective_write_root only read self._project_root +
    # the request write_root; ledger is unused on this path.
    eng = ChangeEngine(project_root=project_root, ledger=object())  # type: ignore[arg-type]

    target = project_root / "qutebrowser" / "browser" / "commands.py"
    redirected = eng._redirect_target(target, worktree)

    assert redirected == worktree / "qutebrowser" / "browser" / "commands.py"
    # Must NOT land in the project (operator) tree.
    assert project_root not in redirected.parents


def test_no_benchmark_write_root_preserves_legacy_target(tmp_path, monkeypatch):
    """write_root=None (every non-swe_bench op) is byte-identical legacy:
    no env override -> target unchanged."""
    monkeypatch.delenv("JARVIS_AUTO_COMMIT_WORKSPACE", raising=False)
    eng = ChangeEngine(project_root=tmp_path, ledger=object())  # type: ignore[arg-type]
    target = tmp_path / "src" / "foo.py"
    assert eng._redirect_target(target, None) == target


# ===========================================================================
# Phase 2a — _strip_test_patch_sections (pure function)
# ===========================================================================

_SRC_SECTION = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    " def f():\n"
    "-    return 1\n"
    "+    return 2\n"
)

_TEST_SECTION = (
    "diff --git a/tests/test_app.py b/tests/test_app.py\n"
    "index 0000000..3333333 100644\n"
    "--- a/tests/test_app.py\n"
    "+++ b/tests/test_app.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def test_f():\n"
    "+    assert f() == 2\n"
)


def test_strips_section_matching_test_path():
    diff = _SRC_SECTION + _TEST_SECTION
    cleaned = _strip_test_patch_sections(diff, ("tests/test_app.py",))
    assert "src/app.py" in cleaned
    assert "return 2" in cleaned
    assert "tests/test_app.py" not in cleaned
    assert "def test_f" not in cleaned


def test_empty_manifest_leaves_diff_untouched():
    diff = _SRC_SECTION + _TEST_SECTION
    assert _strip_test_patch_sections(diff, ()) == diff


def test_all_test_sections_strip_to_empty():
    cleaned = _strip_test_patch_sections(_TEST_SECTION, ("tests/test_app.py",))
    assert cleaned.strip() == ""


def test_path_match_is_exact_not_basename():
    """A source file sharing a basename with a test path must survive —
    we filter on the exact repo-relative path, not the basename."""
    src = (
        "diff --git a/pkg/test_app.py b/pkg/test_app.py\n"
        "--- a/pkg/test_app.py\n"
        "+++ b/pkg/test_app.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )
    cleaned = _strip_test_patch_sections(src, ("tests/test_app.py",))
    assert cleaned == src  # different path -> kept


def test_strips_new_test_file_added_from_dev_null():
    """A newly-added test file (--- /dev/null) is still matched via its
    +++ b/<path> header and stripped."""
    new_test = (
        "diff --git a/tests/new_test.py b/tests/new_test.py\n"
        "new file mode 100644\n"
        "index 0000000..abcdef0\n"
        "--- /dev/null\n"
        "+++ b/tests/new_test.py\n"
        "@@ -0,0 +1 @@\n"
        "+assert True\n"
    )
    cleaned = _strip_test_patch_sections(_SRC_SECTION + new_test, ("tests/new_test.py",))
    assert "src/app.py" in cleaned
    assert "tests/new_test.py" not in cleaned


def test_never_raises_on_garbage_returns_input():
    junk = "not a diff at all\nrandom text\n"
    assert _strip_test_patch_sections(junk, ("tests/x.py",)) == junk


# ===========================================================================
# Phase 2b — capture_produced_patch end-to-end (real git worktree)
# ===========================================================================


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _init_repo(tmp: Path) -> str:
    _git(["init", "-q"], tmp)
    _git(["config", "user.email", "t@t.t"], tmp)
    _git(["config", "user.name", "t"], tmp)
    (tmp / "src").mkdir()
    (tmp / "src" / "app.py").write_text("def f():\n    return 1\n")
    _git(["add", "-A"], tmp)
    _git(["commit", "-q", "-m", "base"], tmp)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp,
                          capture_output=True, text=True).stdout.strip()


def _prepared(tmp: Path, base: str, target_paths=()) -> PreparedProblem:
    return PreparedProblem(
        problem_instance_id="t__inst-1", worktree_path=tmp,
        base_commit=base, repo_url="x/y", branch_name="swebp/t",
        target_paths=tuple(target_paths),
    )


def test_capture_strips_pre_applied_test_patch_footprint(tmp_path):
    """Mirrors the live flow: prepare applied a test_patch (tests/) into the
    worktree, then the model edited source. The captured patch must contain
    ONLY the source change — the test footprint is stripped so the scorer's
    `git apply <test_patch> && git apply <model_patch>` doesn't double-apply."""
    base = _init_repo(tmp_path)
    # Harness pre-applied test_patch: a NEW test file in the working tree.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_f():\n    assert True\n")
    # Model's source edit (uncommitted working tree — Slice 68 substrate).
    (tmp_path / "src" / "app.py").write_text("def f():\n    return 2  # patched\n")

    prepared = _prepared(tmp_path, base, target_paths=("tests/test_app.py",))
    patch, outcome = asyncio.run(capture_produced_patch(prepared))

    assert outcome == DiffCaptureOutcome.CAPTURED, outcome
    assert patch is not None
    assert "src/app.py" in patch
    assert "return 2" in patch
    # The pre-applied test footprint must be GONE.
    assert "tests/test_app.py" not in patch
    assert "def test_f" not in patch


def test_capture_test_only_change_yields_no_changes(tmp_path):
    """If the ONLY working-tree change is the pre-applied test_patch (the model
    produced nothing), filtering strips it to empty -> NO_CHANGES (no false
    CAPTURED of a test-only patch)."""
    base = _init_repo(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_f():\n    assert True\n")

    prepared = _prepared(tmp_path, base, target_paths=("tests/test_app.py",))
    patch, outcome = asyncio.run(capture_produced_patch(prepared))

    assert outcome == DiffCaptureOutcome.NO_CHANGES, outcome
    assert patch is None


def test_capture_source_only_no_manifest_is_unfiltered(tmp_path):
    """Source-only change with an empty manifest: full working-tree capture
    (Slice 68 substrate), nothing stripped."""
    base = _init_repo(tmp_path)
    (tmp_path / "src" / "app.py").write_text("def f():\n    return 3\n")

    prepared = _prepared(tmp_path, base, target_paths=())
    patch, outcome = asyncio.run(capture_produced_patch(prepared))

    assert outcome == DiffCaptureOutcome.CAPTURED, outcome
    assert patch is not None and "return 3" in patch


def test_capture_uncommitted_new_source_file(tmp_path):
    """Slice 68 substrate pin: an ADDED (untracked) source file is captured
    via `git add -A && git diff --cached`."""
    base = _init_repo(tmp_path)
    (tmp_path / "src" / "new_mod.py").write_text("X = 1\n")

    prepared = _prepared(tmp_path, base, target_paths=())
    patch, outcome = asyncio.run(capture_produced_patch(prepared))

    assert outcome == DiffCaptureOutcome.CAPTURED, outcome
    assert patch is not None and "new_mod.py" in patch
