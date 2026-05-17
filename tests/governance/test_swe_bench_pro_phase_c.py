"""Phase C spine — path resolution & orphan-reap hardening.

Proves:
  * Absolute-path invariant — _worktree_path_for() is absolute for the
    relative default AND idempotent for an absolute operator override
    (the bt-2026-05-17-013346 root-cause fix: relative path × cwd=cache
    nested the worktree → spurious test_patch_failed + cache poison)
  * rc-check / surface — worktree-remove failure + persistent orphan
    branch SURFACE as WARNINGs and the canonical `git worktree prune`
    reap fires (no silent orphaning)
  * Outcome relabel — worktree-create failure → WORKTREE_CREATE_FAILED
    (not the legacy CHECKOUT_FAILED misnomer); a genuine apply failure
    still → TEST_PATCH_FAILED (no over-relabel)
  * AST pins — .resolve() in _worktree_path_for; rename complete
    (WORKTREE_CREATE_FAILED present, CHECKOUT_FAILED gone); cleanup
    rc-captured + `worktree prune` present; prepare_problem returns
    WORKTREE_CREATE_FAILED at the wt_pair-None branch

pytest.ini asyncio_mode=auto.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from backend.core.ouroboros.governance.swe_bench_pro import (
    per_problem_harness as pph,
)
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (  # noqa: E501
    HarnessOutcome,
    _worktree_path_for,
    prepare_problem,
    worktree_base_path,
)
from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    ProblemSpec,
)

_SRC = Path(inspect.getfile(pph)).read_text()
_AST = ast.parse(_SRC)
_WT_ENV = "JARVIS_SWE_BENCH_PRO_WORKTREE_BASE_PATH"
_MASTER = "JARVIS_SWE_BENCH_PRO_ENABLED"


def _fn(name):
    for n in ast.walk(_AST):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            n.name == name
        ):
            return n
    raise AssertionError(f"{name} not found")


# ── Absolute-path invariant ────────────────────────────────────────────
def test_worktree_path_absolute_relative_default(monkeypatch):
    monkeypatch.delenv(_WT_ENV, raising=False)
    p = _worktree_path_for("psf__requests-3362")
    assert p.is_absolute()
    assert p == (
        worktree_base_path() / "psf__requests-3362"
    ).resolve()


def test_worktree_path_absolute_with_absolute_override(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(_WT_ENV, str(tmp_path / "wt"))
    p = _worktree_path_for("django__django-16255")
    assert p.is_absolute()
    assert p == (tmp_path / "wt" / "django__django-16255").resolve()


def test_worktree_path_never_nests_in_cwd_relative(monkeypatch):
    # The defect: a relative path. Assert the result is NOT relative.
    monkeypatch.delenv(_WT_ENV, raising=False)
    p = _worktree_path_for("x__y-1")
    assert p.is_absolute() and ".." not in p.parts


# ── rc-check / surface (no silent orphan) ──────────────────────────────
async def test_cleanup_rc_surfaces_and_prunes(
    monkeypatch, tmp_path, caplog
):
    calls: list[list[str]] = []
    wt_dir = tmp_path / "wt" / "inst-1"
    wt_dir.mkdir(parents=True)
    monkeypatch.setattr(pph, "worktree_base_path", lambda: tmp_path / "wt")

    async def fake_run_git(args, cwd=None, stdin_input=None, timeout_s=None):
        calls.append(list(args))
        if args[:2] == ["worktree", "remove"]:
            return 1, "", "fatal: cannot remove"          # FAIL → surface
        if args[:2] == ["branch", "-D"]:
            return 1, "", "error: not found"
        if args[:2] == ["branch", "--list"]:
            return 0, "  inst-1\n", ""                      # orphan persists
        if args[:2] == ["worktree", "add"]:
            return 1, "", "fatal: already exists"
        return 0, "", ""

    monkeypatch.setattr(pph, "_run_git", fake_run_git)
    import logging
    with caplog.at_level(logging.WARNING):
        out = await pph._create_problem_worktree(
            tmp_path / "cache", "deadbeef", "inst-1"
        )
    assert out is None
    log = caplog.text
    assert "worktree remove rc=1" in log, "remove failure must surface"
    assert "orphan branch" in log and "persists" in log, (
        "persistent orphan must surface, not silently proceed"
    )
    assert ["worktree", "prune"] in calls, (
        "canonical L3-mirror reap (`git worktree prune`) must fire"
    )


# ── Outcome relabel ────────────────────────────────────────────────────
async def test_relabel_worktree_create_failed(monkeypatch):
    monkeypatch.setenv(_MASTER, "true")

    async def _ok_cache(url):
        return Path("/tmp/cache")

    async def _wt_none(c, b, i):
        return None

    monkeypatch.setattr(pph, "_ensure_repo_cached", _ok_cache)
    monkeypatch.setattr(pph, "_create_problem_worktree", _wt_none)
    spec = ProblemSpec(
        instance_id="i-1", repo="o/r", repo_url="file:///x",
        base_commit="abc", problem_statement="", test_patch="",
        gold_patch="",
    )
    result, outcome = await prepare_problem(spec)
    assert result is None
    assert outcome is HarnessOutcome.WORKTREE_CREATE_FAILED
    assert not hasattr(HarnessOutcome, "CHECKOUT_FAILED")


async def test_no_over_relabel_apply_failure_still_test_patch_failed(
    monkeypatch
):
    monkeypatch.setenv(_MASTER, "true")

    async def _ok_cache(url):
        return Path("/tmp/cache")

    async def _wt_ok(c, b, i):
        return Path("/tmp/wt"), "br-1"

    async def _apply_fail(wt, patch):
        return False

    monkeypatch.setattr(pph, "_ensure_repo_cached", _ok_cache)
    monkeypatch.setattr(pph, "_create_problem_worktree", _wt_ok)
    monkeypatch.setattr(pph, "_apply_test_patch", _apply_fail)
    spec = ProblemSpec(
        instance_id="i-2", repo="o/r", repo_url="file:///x",
        base_commit="abc", problem_statement="", test_patch="diff x",
        gold_patch="",
    )
    result, outcome = await prepare_problem(spec)
    assert result is None
    assert outcome is HarnessOutcome.TEST_PATCH_FAILED  # NOT over-relabeled


# ── AST pins ───────────────────────────────────────────────────────────
def test_ast_pin_worktree_path_resolved():
    body = ast.unparse(_fn("_worktree_path_for"))
    assert ".resolve()" in body, (
        "_worktree_path_for MUST .resolve() to absolute (root-cause)"
    )


def test_ast_pin_rename_complete():
    names = {
        a.targets[0].id
        for n in ast.walk(_AST)
        if isinstance(n, ast.ClassDef) and n.name == "HarnessOutcome"
        for a in n.body
        if isinstance(a, ast.Assign) and isinstance(a.targets[0], ast.Name)
    }
    assert "WORKTREE_CREATE_FAILED" in names
    assert "CHECKOUT_FAILED" not in names, "legacy misnomer must be gone"


def test_ast_pin_cleanup_rc_captured_and_prunes():
    fn = _fn("_create_problem_worktree")
    src = ast.unparse(fn)
    # canonical L3-mirror reap present
    assert "'worktree', 'prune'" in src or '"worktree", "prune"' in src, (
        "cleanup must invoke `git worktree prune`"
    )
    # rc captured (assignment) for the removal commands — not discarded
    assigns = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Tuple) for t in n.targets
        )
        and isinstance(n.value, ast.Await)
    ]
    assert len(assigns) >= 3, (
        "worktree-remove / branch-D / branch--list _run_git results "
        "must be rc-captured (>=3 tuple-assigned awaits), not bare"
    )


def test_ast_pin_prepare_returns_worktree_create_failed():
    src = ast.unparse(_fn("prepare_problem"))
    assert "HarnessOutcome.WORKTREE_CREATE_FAILED" in src
    assert "HarnessOutcome.CHECKOUT_FAILED" not in src
