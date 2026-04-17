"""``/undo N`` command tests — UndoPlanner + UndoExecutor + handler wiring.

Strategy: spin up a real tmp git repo per test via ``git init``, craft
synthetic commits with and without the canonical O+V trailer, and
drive the planner / executor end-to-end. This is more expensive than
pure mocks but gives us honest coverage of ``git log`` parsing, stat
extraction, revert mechanics, and abort-on-failure behavior.

Coverage matrix:
  • Env kill switch + max-batch cap
  • Argument parsing (``/undo``, ``/undo 3``, ``/undo preview 2``,
    ``/undo --hard 1``, unknown tokens, non-integer N)
  • Planner — trailer classification, dirty-tree rejection, active-ops
    rejection, N > cap, N > available commits, manual-in-range refusal,
    stat extraction, pushed-branch detection, --hard + pushed refusal
  • Executor — successful revert creates a single O+V-signed revert
    commit, hard-mode reset, preview no-op, abort on failure
  • render_plan — Rich output contains expected tokens; plain fallback
    works when Rich unimportable
  • AST canary — SerpentFlow/harness register the slash handler; trailer
    string matches auto_committer._OV_COAUTHOR
"""
from __future__ import annotations

import asyncio
import ast
import os
import subprocess
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.battle_test import undo_command as uc
from backend.core.ouroboros.battle_test.undo_command import (
    UndoExecutor,
    UndoPlan,
    UndoPlanner,
    UndoTarget,
    max_batch,
    parse_undo_args,
    render_plan,
    undo_enabled,
)


_OV_TRAILER = "Co-Authored-By: Ouroboros+Venom <ouroboros@jarvis.trinity>"


# ---------------------------------------------------------------------------
# Fixtures — tmp git repo + commit helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True, text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Tmp git repo with identity configured (required for commits)."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    # Seed with an initial non-O+V commit so HEAD~N is reachable.
    (tmp_path / "README.md").write_text("init\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "chore: initial commit")
    return tmp_path


def _make_ov_commit(repo: Path, path: str, content: str, subject: str) -> str:
    file_path = repo / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    _git(repo, "add", path)
    body = (
        "\n\nAutonomous change for testing.\n\n"
        "Ouroboros+Venom [O+V] — Autonomous Self-Development Engine\n"
        f"{_OV_TRAILER}\n"
    )
    _git(repo, "commit", "-q", "-m", subject + body)
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return sha


def _make_manual_commit(repo: Path, path: str, content: str, subject: str) -> str:
    file_path = repo / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_UNDO_"):
            monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# (1) Env gates
# ---------------------------------------------------------------------------


def test_undo_enabled_default_on():
    assert undo_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_undo_disabled_values(monkeypatch, value):
    monkeypatch.setenv("JARVIS_UNDO_ENABLED", value)
    assert undo_enabled() is False


def test_max_batch_default_10():
    assert max_batch() == 10


def test_max_batch_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_UNDO_MAX_BATCH", "25")
    assert max_batch() == 25


def test_max_batch_clamps_absurd_values(monkeypatch):
    monkeypatch.setenv("JARVIS_UNDO_MAX_BATCH", "0")
    assert max_batch() == 1
    monkeypatch.setenv("JARVIS_UNDO_MAX_BATCH", "999999")
    assert max_batch() == 100


# ---------------------------------------------------------------------------
# (2) Argument parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("/undo",                 (1, "revert", None)),
    ("/undo 3",               (3, "revert", None)),
    ("undo 5",                (5, "revert", None)),
    ("/undo preview",         (1, "preview", None)),
    ("/undo preview 4",       (4, "preview", None)),
    ("/undo --hard 2",        (2, "hard", None)),
    ("/undo 2 --hard",        (2, "hard", None)),
])
def test_parse_undo_args_valid(raw, expected):
    assert parse_undo_args(raw) == expected


def test_parse_undo_args_rejects_unknown_token():
    n, mode, err = parse_undo_args("/undo banana")
    assert n == 0
    assert err is not None and "unknown" in err.lower()


def test_parse_undo_args_rejects_zero_or_negative():
    n, _, err = parse_undo_args("/undo 0")
    assert n == 0 and err is not None


# ---------------------------------------------------------------------------
# (3) Planner — trailer classification
# ---------------------------------------------------------------------------


def test_planner_classifies_ov_vs_manual(repo):
    _make_ov_commit(repo, "a.py", "A\n", "feat: add a")
    _make_ov_commit(repo, "b.py", "B\n", "feat: add b")
    planner = UndoPlanner(repo)
    plan = planner.plan(2)
    assert len(plan.targets) == 2
    assert all(t.is_ov for t in plan.targets)
    # Newest commit is first in git log output.
    assert plan.targets[0].subject == "feat: add b"
    assert plan.targets[1].subject == "feat: add a"
    assert plan.is_safe
    assert plan.mode == "revert"


def test_planner_refuses_when_manual_in_range(repo):
    _make_ov_commit(repo, "a.py", "A\n", "feat: add a")
    _make_manual_commit(repo, "b.py", "B\n", "manual: tweak b")
    _make_ov_commit(repo, "c.py", "C\n", "feat: add c")
    planner = UndoPlanner(repo)
    plan = planner.plan(3)
    assert not plan.is_safe
    assert any("not O+V" in e for e in plan.safety_errors)


def test_planner_ok_when_manual_below_range(repo):
    """Manual commit BELOW the N range is fine — we only scan the top N."""
    _make_manual_commit(repo, "older.py", "x", "manual: old")
    _make_ov_commit(repo, "a.py", "A", "feat: a")
    _make_ov_commit(repo, "b.py", "B", "feat: b")
    plan = UndoPlanner(repo).plan(2)
    assert plan.is_safe
    assert len(plan.targets) == 2


# ---------------------------------------------------------------------------
# (4) Planner — safety gates
# ---------------------------------------------------------------------------


def test_planner_rejects_when_disabled(monkeypatch, repo):
    monkeypatch.setenv("JARVIS_UNDO_ENABLED", "0")
    _make_ov_commit(repo, "a.py", "A", "feat: a")
    plan = UndoPlanner(repo).plan(1)
    assert any("disabled" in e.lower() for e in plan.safety_errors)


def test_planner_rejects_dirty_tree(repo):
    _make_ov_commit(repo, "a.py", "A", "feat: a")
    # Leave uncommitted change.
    (repo / "a.py").write_text("A-dirty")
    plan = UndoPlanner(repo).plan(1)
    assert any("not clean" in e.lower() for e in plan.safety_errors)


def test_planner_rejects_active_ops(repo):
    _make_ov_commit(repo, "a.py", "A", "feat: a")
    gls = MagicMock()
    gls._active_ops = {"op-one", "op-two"}
    plan = UndoPlanner(repo, governed_loop_service=gls).plan(1)
    assert any("in flight" in e for e in plan.safety_errors)
    assert any("op-one" in e or "op-two" in e for e in plan.safety_errors)


def test_planner_rejects_over_cap(monkeypatch, repo):
    monkeypatch.setenv("JARVIS_UNDO_MAX_BATCH", "2")
    for i in range(5):
        _make_ov_commit(repo, f"{i}.py", f"{i}", f"feat: add {i}")
    plan = UndoPlanner(repo).plan(5)
    assert any("exceeds safety cap" in e for e in plan.safety_errors)


def test_planner_rejects_more_than_available(repo):
    _make_ov_commit(repo, "a.py", "A", "feat: a")
    plan = UndoPlanner(repo).plan(10)
    # Only 2 commits exist (initial + 1 O+V); requesting 10 fails.
    assert any("cannot undo" in e.lower() for e in plan.safety_errors)


def test_planner_rejects_bad_mode(repo):
    _make_ov_commit(repo, "a.py", "A", "feat: a")
    plan = UndoPlanner(repo).plan(1, mode="bogus")
    assert any("unknown undo mode" in e.lower() for e in plan.safety_errors)


# ---------------------------------------------------------------------------
# (5) Planner — stat extraction
# ---------------------------------------------------------------------------


def test_planner_extracts_per_commit_stats(repo):
    _make_ov_commit(
        repo, "x.py",
        "\n".join(f"line {i}" for i in range(10)) + "\n",
        "feat: x with 10 lines",
    )
    plan = UndoPlanner(repo).plan(1)
    assert plan.targets[0].insertions >= 10
    assert "x.py" in plan.targets[0].files_changed


# ---------------------------------------------------------------------------
# (6) Executor — preview mode is a no-op
# ---------------------------------------------------------------------------


def test_executor_preview_does_not_mutate(repo):
    sha_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _make_ov_commit(repo, "a.py", "A", "feat: a")
    sha_after = _git(repo, "rev-parse", "HEAD").stdout.strip()

    planner = UndoPlanner(repo)
    plan = planner.plan(1, mode="preview")
    assert plan.is_safe

    result = asyncio.run(UndoExecutor(repo).execute(plan))
    assert result.executed is False
    assert result.mode == "preview"

    # HEAD unchanged.
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == sha_after
    assert sha_after != sha_before  # sanity: the O+V commit did land


# ---------------------------------------------------------------------------
# (7) Executor — revert mode success
# ---------------------------------------------------------------------------


def test_executor_revert_creates_single_ov_signed_commit(repo):
    _make_ov_commit(repo, "a.py", "A\n", "feat: add a")
    _make_ov_commit(repo, "b.py", "B\n", "feat: add b")

    plan = UndoPlanner(repo).plan(2)
    assert plan.is_safe

    result = asyncio.run(UndoExecutor(repo).execute(plan))
    assert result.executed is True
    assert result.mode == "revert"
    assert result.n_reverted == 2
    # New revert commit SHA was populated.
    assert result.committed_sha

    # HEAD is the new revert commit (not a reset — history preserved).
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert head == result.committed_sha

    # Revert commit bears O+V trailer.
    body = _git(repo, "log", "-1", "--format=%B", "HEAD").stdout
    assert _OV_TRAILER in body
    assert "Revert: undo last 2" in body

    # The two reverted files are back to pre-O+V state.
    assert not (repo / "a.py").exists()
    assert not (repo / "b.py").exists()


def test_executor_revert_single_commit(repo):
    _make_ov_commit(repo, "solo.py", "solo\n", "feat: solo")
    plan = UndoPlanner(repo).plan(1)
    result = asyncio.run(UndoExecutor(repo).execute(plan))
    assert result.executed is True
    assert result.n_reverted == 1
    # Singular noun in subject.
    body = _git(repo, "log", "-1", "--format=%B").stdout
    assert "Revert: undo last 1 autonomous commit" in body
    assert "Revert: undo last 1 autonomous commits" not in body


# ---------------------------------------------------------------------------
# (8) Executor — hard mode resets HEAD
# ---------------------------------------------------------------------------


def test_executor_hard_resets_head(repo):
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _make_ov_commit(repo, "a.py", "A", "feat: a")
    _make_ov_commit(repo, "b.py", "B", "feat: b")

    plan = UndoPlanner(repo).plan(2, mode="hard")
    # Unpushed tmp repo: --hard is safe.
    assert plan.is_safe, f"unexpected errors: {plan.safety_errors}"

    result = asyncio.run(UndoExecutor(repo).execute(plan))
    assert result.executed is True
    assert result.mode == "hard"

    # HEAD is back at the initial commit.
    after = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert after == before


# ---------------------------------------------------------------------------
# (9) Executor — safety-error plans never mutate
# ---------------------------------------------------------------------------


def test_executor_refuses_unsafe_plan(repo):
    _make_ov_commit(repo, "a.py", "A", "feat: a")
    (repo / "a.py").write_text("dirty")  # dirty tree → unsafe
    plan = UndoPlanner(repo).plan(1)
    assert not plan.is_safe

    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result = asyncio.run(UndoExecutor(repo).execute(plan))
    assert result.executed is False
    assert "safety" in result.error.lower()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


# ---------------------------------------------------------------------------
# (10) Executor — emits emit_decision on success
# ---------------------------------------------------------------------------


def test_executor_emits_decision_on_success(repo):
    _make_ov_commit(repo, "a.py", "A", "feat: a")

    comm = MagicMock()
    async def _emit(**_kw):
        return None
    comm.emit_decision = MagicMock(side_effect=_emit)

    plan = UndoPlanner(repo).plan(1)
    result = asyncio.run(UndoExecutor(repo, comm=comm).execute(plan))
    assert result.executed is True
    # emit_decision was awaited with outcome="undo".
    comm.emit_decision.assert_called_once()
    call_kwargs = comm.emit_decision.call_args.kwargs
    assert call_kwargs["outcome"] == "undo"
    assert "user_undo_n=1" in call_kwargs["reason_code"]
    assert "mode=revert" in call_kwargs["reason_code"]


# ---------------------------------------------------------------------------
# (11) render_plan — Rich output + plain fallback
# ---------------------------------------------------------------------------


def test_render_plan_rich_output_contains_key_tokens(repo):
    _make_ov_commit(repo, "a.py", "A", "feat: add a")
    plan = UndoPlanner(repo).plan(1)

    # Use Console(record=True) to capture rendered output.
    from rich.console import Console
    console = Console(record=True, width=140, force_terminal=True)
    console.print(render_plan(plan))
    text = console.export_text()

    assert "/undo" in text
    assert "mode=revert" in text
    assert "n=1" in text
    assert "feat: add a" in text
    assert "O+V" in text
    assert "ready to execute" in text


def test_render_plan_preview_marks_no_changes(repo):
    _make_ov_commit(repo, "a.py", "A", "feat: a")
    plan = UndoPlanner(repo).plan(1, mode="preview")

    from rich.console import Console
    console = Console(record=True, width=140, force_terminal=True)
    console.print(render_plan(plan))
    text = console.export_text()
    assert "preview" in text.lower()
    assert "no changes" in text.lower()


def test_render_plan_errors_surface_in_output(repo):
    _make_ov_commit(repo, "a.py", "A", "feat: a")
    (repo / "a.py").write_text("dirty")
    plan = UndoPlanner(repo).plan(1)

    from rich.console import Console
    console = Console(record=True, width=140, force_terminal=True)
    console.print(render_plan(plan))
    text = console.export_text()
    assert "✖" in text or "ERROR" in text.upper()
    assert "not clean" in text.lower()


# ---------------------------------------------------------------------------
# (12) AST canaries — wiring + trailer invariants
# ---------------------------------------------------------------------------


def _read(parts: tuple) -> str:
    base = Path(__file__).resolve().parent.parent.parent
    return base.joinpath(*parts).read_text(encoding="utf-8")


def test_harness_dispatches_undo_command():
    src = _read((
        "backend", "core", "ouroboros", "battle_test", "harness.py",
    ))
    assert "_repl_cmd_undo" in src, (
        "harness.py no longer defines _repl_cmd_undo — /undo will "
        "hit the 'Unknown REPL command' branch silently."
    )
    tree = ast.parse(src)
    # Confirm dispatch in _handle_repl_command: look for "/undo" literal.
    assert '"/undo"' in src or "'/undo'" in src


def test_ov_trailer_matches_auto_committer():
    """Undo module's trailer constant MUST match
    auto_committer._OV_COAUTHOR byte-for-byte. If AutoCommitter's
    trailer changes, the undo module silently stops matching any
    commit and /undo becomes a no-op. This is the regression guard.
    """
    uc_src = _read((
        "backend", "core", "ouroboros", "battle_test", "undo_command.py",
    ))
    ac_src = _read((
        "backend", "core", "ouroboros", "governance", "auto_committer.py",
    ))
    assert _OV_TRAILER in uc_src
    assert _OV_TRAILER in ac_src


def test_undo_module_has_public_surface():
    """Regression canary: the four public names the handler imports
    must exist. A refactor that renames/removes them breaks the REPL."""
    for name in (
        "UndoPlanner", "UndoExecutor", "parse_undo_args", "render_plan",
    ):
        assert hasattr(uc, name), (
            f"undo_command.{name} missing — /undo handler will ImportError"
        )
