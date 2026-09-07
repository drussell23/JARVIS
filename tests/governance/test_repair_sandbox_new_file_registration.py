"""RepairSandbox new-file candidate registration (git intent-to-add).

Root cause (2026-09-07): ``tests/conftest.py`` amputates every path that
``git ls-files`` does not report. A brand-new candidate file written into
the worktree sandbox was untracked there, so VALIDATE ran ZERO tests on a
perfectly good candidate ("no tests ran" -> FAIL) on every retry. The
sandbox now registers each materialized candidate path with the sandbox
index as intent-to-add -- git's own "exists, no content staged" primitive
-- so index-driven collection sees it while nothing is staged for commit.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import textwrap
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.repair_sandbox import RepairSandbox

_LOGGER = "backend.core.ouroboros.governance.repair_sandbox"

# A minimal replica of the repo conftest's untracked-artifact amputation:
# refuse any FILE that ``git ls-files`` does not report, abstain when git
# cannot answer. This is what turned a good new-file candidate into
# "no tests ran".
_AMPUTATING_CONFTEST = textwrap.dedent(
    '''
    import subprocess
    from pathlib import Path

    def pytest_ignore_collect(collection_path=None, path=None, config=None):
        p = Path(str(collection_path if collection_path is not None else path))
        if p.is_dir():
            return None
        root = Path(__file__).resolve().parent
        out = subprocess.run(["git", "ls-files", "-z"], cwd=str(root),
                             capture_output=True, check=False)
        if out.returncode != 0:
            return None
        tracked = {n for n in out.stdout.decode().split("\\0") if n}
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            return None
        return None if rel in tracked else True
    '''
)


@pytest.fixture
def repo(tmp_path):
    """A tiny git repo whose committed conftest amputates untracked tests."""
    r = tmp_path / "repo"
    r.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (r / "conftest.py").write_text(_AMPUTATING_CONFTEST)
    (r / "tracked.py").write_text("x = 1\n")
    (r / ".gitignore").write_text("ignored/\n")
    (r / "tests").mkdir()
    (r / "tests" / "__init__.py").write_text("")
    git("add", "-A")
    git("commit", "-qm", "base")
    return r


def _ls_files(root: Path) -> set:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                         capture_output=True, check=True)
    return {n for n in out.stdout.decode().split("\0") if n}


def _staged(root: Path) -> set:
    out = subprocess.run(["git", "diff", "--cached", "--name-only"],
                         cwd=root, capture_output=True, check=True)
    return {n for n in out.stdout.decode().split("\n") if n}


def test_new_file_via_full_content_is_visible_to_ls_files(repo):
    async def _run():
        async with RepairSandbox(repo, 30.0) as sb:
            root = sb.sandbox_root
            assert "tests/test_new.py" not in _ls_files(root)
            await sb.apply_full_content("def test_a():\n    assert True\n",
                                        "tests/test_new.py")
            assert (root / "tests" / "test_new.py").is_file()
            assert "tests/test_new.py" in _ls_files(root)
            # Intent-to-add stages NOTHING for commit.
            assert _staged(root) == set()
    asyncio.run(_run())


def test_new_file_via_patch_is_visible_to_ls_files(repo):
    diff = "@@ -0,0 +1,2 @@\n+def test_p():\n+    assert True\n"

    async def _run():
        async with RepairSandbox(repo, 30.0) as sb:
            await sb.apply_patch(diff, "tests/test_patched.py")
            assert "tests/test_patched.py" in _ls_files(sb.sandbox_root)
    asyncio.run(_run())


def test_tracked_file_registration_is_idempotent(repo, caplog):
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    async def _run():
        async with RepairSandbox(repo, 30.0) as sb:
            await sb.apply_full_content("x = 2\n", "tracked.py")
            assert "tracked.py" in _ls_files(sb.sandbox_root)
            assert (sb.sandbox_root / "tracked.py").read_text() == "x = 2\n"
    asyncio.run(_run())
    assert not [r for r in caplog.records if "intent-to-add" in r.getMessage()]


def test_ignored_path_is_written_but_refusal_is_logged(repo, caplog):
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    async def _run():
        async with RepairSandbox(repo, 30.0) as sb:
            await sb.apply_full_content("y = 1\n", "ignored/test_i.py")
            assert (sb.sandbox_root / "ignored" / "test_i.py").is_file()
            assert "ignored/test_i.py" not in _ls_files(sb.sandbox_root)
    asyncio.run(_run())
    msgs = [r.getMessage() for r in caplog.records]
    assert any("intent-to-add refused for ignored/test_i.py" in m for m in msgs)


def test_rsync_mode_skips_registration_silently(repo, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    async def _boom(self, tmpdir):
        raise RuntimeError("forced worktree failure")

    monkeypatch.setattr(RepairSandbox, "_git_worktree_add", _boom)

    async def _run():
        async with RepairSandbox(repo, 30.0) as sb:
            assert not (sb.sandbox_root / ".git").exists()
            await sb.apply_full_content("def test_r():\n    assert True\n",
                                        "tests/test_r.py")
            assert (sb.sandbox_root / "tests" / "test_r.py").is_file()
    asyncio.run(_run())
    assert not [r for r in caplog.records if "intent-to-add" in r.getMessage()]


def test_end_to_end_new_test_file_is_collected_and_passes(repo):
    """The regression itself: under an amputating conftest, a brand-new
    candidate test file must be COLLECTED and RUN inside the sandbox."""
    async def _run():
        async with RepairSandbox(repo, 60.0) as sb:
            await sb.apply_full_content(
                "def test_new_passes():\n    assert 1 + 1 == 2\n",
                "tests/test_new.py",
            )
            return await sb.run_tests(("tests/test_new.py",), timeout_s=60.0)
    res = asyncio.run(_run())
    assert res.passed, res.stdout + res.stderr
    assert "1 passed" in res.stdout, res.stdout
    assert "no tests ran" not in res.stdout
