"""
Regression spine for the native git-hook enforcement of the Autonomic Git-Mutex.

The mutex in :mod:`backend.core.git_transaction_lock` binds only processes that
call it. These tests prove the escalation: a **raw** ``git`` command — one that
knows nothing about the mutex — is aborted by git itself when another agent
holds the lock.

Everything here drives real repositories, real installed hooks, and real
subprocesses. The lock is genuinely held (not patched), and the child's
environment is stripped of ``JARVIS_GIT_TXN_TOKEN`` so it presents as a
*different* agent — which is precisely the 2026-08-02 scenario.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from backend.core import git_transaction_lock as gtl
from backend.core.git_mutex_hook import ALLOW, BLOCK, decide
from backend.core.git_transaction_lock import git_transaction, probe_lock

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_SRC = REPO_ROOT / "scripts" / "hooks"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _foreign_env(repo_root: Path) -> dict:
    """Env for a subprocess that must look like a DIFFERENT agent.

    Dropping JARVIS_GIT_TXN_TOKEN is the whole point: with the token present the
    hook correctly recognises the caller as the lock owner and lets it through.
    """
    env = dict(os.environ)
    env.pop(gtl.TOKEN_ENV, None)
    env["JARVIS_GIT_MUTEX_HOOK_ROOT"] = str(repo_root)
    env["PYTHONPATH"] = str(repo_root)
    return env


@pytest.fixture
def hooked_repo(tmp_path: Path) -> Path:
    """A real git repo with the Git-Mutex hooks actually installed."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "seed.txt").write_text("seed\n")
    _git(r, "add", "seed.txt")
    _git(r, "commit", "-q", "-m", "seed")

    hooks = r / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    for name in ("_git_mutex_bridge.sh", "reference-transaction", "pre-rebase"):
        dst = hooks / name
        shutil.copy2(HOOK_SRC / name, dst)
        dst.chmod(0o755)
    return r


@pytest.fixture(autouse=True)
def _clean_manager_cache():
    gtl._managers.clear()
    yield
    gtl._managers.clear()


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def test_reference_transaction_only_vetoes_prepared_state():
    """Blocking at 'committed' would be theatre — the refs already moved."""
    assert decide("reference-transaction", ["committed"]) == ALLOW
    assert decide("reference-transaction", ["aborted"]) == ALLOW


def test_hook_allows_when_no_lock_held(hooked_repo: Path, monkeypatch):
    monkeypatch.chdir(hooked_repo)
    assert probe_lock().held_by_other is False
    assert decide("reference-transaction", ["prepared"]) == ALLOW


def test_master_switch_disables_hook(hooked_repo: Path, monkeypatch):
    monkeypatch.chdir(hooked_repo)
    monkeypatch.setenv("JARVIS_GIT_HOOK_ENABLED", "false")
    assert decide("reference-transaction", ["prepared"]) == ALLOW


# ---------------------------------------------------------------------------
# Fail-open: a broken hook must never brick the repository
# ---------------------------------------------------------------------------

def test_block_sentinel_is_not_plain_one():
    """A refusal must be distinguishable from 'python failed to start'.

    Both a missing module and a broken venv exit 1. If BLOCK were also 1, the
    bridge could not tell them apart and any interpreter problem would refuse
    every git operation in the repository.
    """
    assert BLOCK == 17
    assert BLOCK != 1
    assert ALLOW == 0


def _run_bridge(hook: str, arg: str, env_extra: dict, cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop(gtl.TOKEN_ENV, None)
    env["GIT_MUTEX_HOOK_NAME"] = hook
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(HOOK_SRC / "_git_mutex_bridge.sh"), arg],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
    )


def test_bridge_fails_open_when_module_cannot_be_imported(hooked_repo: Path, tmp_path: Path):
    """Module absent at the resolved root -> allow, not block.

    Regression: before the sentinel existed, this path exited 1 and would have
    refused every ref transaction in the repository.
    """
    empty = tmp_path / "no_jarvis_here"
    (empty / "backend").mkdir(parents=True)  # passes the -d guard, has no package
    res = _run_bridge("reference-transaction", "prepared",
                      {"JARVIS_GIT_MUTEX_HOOK_ROOT": str(empty)}, hooked_repo)
    assert res.returncode == 0, f"bricking failure mode returned:\n{res.stderr}"
    assert "could not evaluate" in res.stderr


def test_bridge_strict_mode_refuses_on_internal_failure(hooked_repo: Path, tmp_path: Path):
    empty = tmp_path / "no_jarvis_strict"
    (empty / "backend").mkdir(parents=True)
    res = _run_bridge("reference-transaction", "prepared",
                      {"JARVIS_GIT_MUTEX_HOOK_ROOT": str(empty),
                       "JARVIS_GIT_HOOK_STRICT": "true"}, hooked_repo)
    assert res.returncode == 1


def test_bridge_evaluates_when_root_differs_from_cwd(hooked_repo: Path):
    """`-m` would resolve `backend` against cwd (sys.path[0]) and miss the root."""
    res = _run_bridge("reference-transaction", "prepared",
                      {"JARVIS_GIT_MUTEX_HOOK_ROOT": str(REPO_ROOT)}, hooked_repo)
    assert res.returncode == 0
    assert "could not evaluate" not in res.stderr, (
        "hook silently failed to import instead of deciding:\n" + res.stderr
    )


# ---------------------------------------------------------------------------
# The mandate: a raw `git commit` must fail while another agent holds the lock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_raw_git_commit_blocked_by_hook_while_lock_held(hooked_repo: Path):
    """A git command that never heard of the mutex is aborted by git itself."""
    (hooked_repo / "intruder.txt").write_text("written by another agent\n")
    _git(hooked_repo, "add", "intruder.txt")
    head_before = _git(hooked_repo, "rev-parse", "HEAD").stdout.strip()

    async with git_transaction("holder", cwd=hooked_repo):
        proc = await asyncio.create_subprocess_exec(
            "git", "commit", "-m", "should be refused",
            cwd=str(hooked_repo),
            env=_foreign_env(REPO_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        rc = proc.returncode

    combined = (out + err).decode(errors="replace")
    assert rc != 0, f"raw git commit succeeded despite held lock:\n{combined}"
    assert "GIT-MUTEX" in combined, f"hook banner absent:\n{combined[:1500]}"

    head_after = _git(hooked_repo, "rev-parse", "HEAD").stdout.strip()
    assert head_after == head_before, "HEAD moved despite the hook blocking"


@pytest.mark.asyncio
async def test_same_commit_succeeds_once_the_lock_is_released(hooked_repo: Path):
    """The hook must gate, not brick: the identical command passes when free."""
    (hooked_repo / "later.txt").write_text("ok\n")
    _git(hooked_repo, "add", "later.txt")
    head_before = _git(hooked_repo, "rev-parse", "HEAD").stdout.strip()

    async with git_transaction("holder", cwd=hooked_repo):
        pass  # acquire and release

    proc = await asyncio.create_subprocess_exec(
        "git", "commit", "-m", "allowed now",
        cwd=str(hooked_repo),
        env=_foreign_env(REPO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    combined = (out + err).decode(errors="replace")
    assert proc.returncode == 0, f"commit refused with no lock held:\n{combined}"
    assert _git(hooked_repo, "rev-parse", "HEAD").stdout.strip() != head_before


@pytest.mark.asyncio
async def test_lock_holder_is_not_blocked_by_its_own_lock(hooked_repo: Path):
    """Self-exclusion: the owner's git children carry the token and pass.

    Without this the mutex would deadlock every caller against itself.
    """
    (hooked_repo / "mine.txt").write_text("owner's work\n")

    async with git_transaction("owner", cwd=hooked_repo):
        assert os.environ.get(gtl.TOKEN_ENV), "token not published during hold"
        env = dict(os.environ)          # token retained -> recognised as owner
        env["JARVIS_GIT_MUTEX_HOOK_ROOT"] = str(REPO_ROOT)
        env["PYTHONPATH"] = str(REPO_ROOT)
        for argv in (["git", "add", "mine.txt"], ["git", "commit", "-m", "own work"]):
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(hooked_repo), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
            assert proc.returncode == 0, (
                f"holder blocked by its own lock: {argv}\n"
                + (out + err).decode(errors="replace")
            )


def test_raw_git_commit_blocked_with_lock_held_by_background_thread(hooked_repo: Path):
    """The mandate's shape: holder on a background thread, raw git on the main one."""
    acquired = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def holder() -> None:
        async def run() -> None:
            async with git_transaction("bg-holder", cwd=hooked_repo):
                acquired.set()
                await asyncio.get_running_loop().run_in_executor(None, release.wait)
        try:
            asyncio.run(run())
        except BaseException as exc:  # noqa: BLE001 — surfaced to the assertion
            errors.append(exc)
            acquired.set()

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert acquired.wait(timeout=60), "background holder never acquired"
    assert not errors, f"holder thread failed: {errors!r}"

    try:
        (hooked_repo / "x.txt").write_text("x\n")
        subprocess.run(["git", "add", "x.txt"], cwd=hooked_repo,
                       env=_foreign_env(REPO_ROOT), capture_output=True, check=False)
        res = subprocess.run(
            ["git", "commit", "-m", "blocked"],
            cwd=hooked_repo, env=_foreign_env(REPO_ROOT),
            capture_output=True, text=True, timeout=120,
        )
        assert res.returncode != 0, (
            f"raw git commit succeeded while background thread held the lock:\n"
            f"{res.stdout}\n{res.stderr}"
        )
        assert "GIT-MUTEX" in (res.stdout + res.stderr)
    finally:
        release.set()
        t.join(timeout=60)
