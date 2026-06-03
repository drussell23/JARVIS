"""Slice 76 — Resilient Ingress: concurrent same-repo clone serialization + retry.

Root cause (EVAL-2, session bt-2026-06-03-094511, PRD §50.11): the two ansible
instances are the same upstream repo (`ansible/ansible`) at different commits. With
a cold cache and 3 background workers, their two `_ensure_repo_cached` calls raced
into the *same* cache path concurrently — both fell through the cache-hit fast-path
(no valid `.git` yet), and the second's `git clone` aborted with
``rc=128: destination path '…' already exists and is not an empty directory``. The
NodeBB pair did NOT race only because its cache already existed (cache-hit → no
fresh clone). Verify-first against the live error proved this is a *concurrency race
on a shared cache path*, NOT the runbook's hypothesised rate-limiting / socket drop.

The structural fix (verified here before implementation):
  1. A per-repo-path ``asyncio.Lock`` SERIALIZES same-repo clones — the second
     caller waits, then takes the now-valid cache-hit (exactly one real clone).
  2. Bounded purge-and-retry is defense-in-depth for a genuinely stale / partial
     cache dir (a crashed prior run leaving a non-empty non-``.git`` directory):
     intercept ``rc=128``, purge the dir, jittered backoff, re-clone.
  3. The lock is keyed PER REPO PATH, so *different* repos still clone concurrently
     (no global serialization stall).
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from backend.core.ouroboros.governance.swe_bench_pro import per_problem_harness
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (
    _ensure_repo_cached,
    REPO_CACHE_PATH_ENV_VAR,
)


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setenv(REPO_CACHE_PATH_ENV_VAR, str(tmp_path / "cache"))
    # zero backoff so retry tests run instantly; clear any per-process lock state
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_CLONE_RETRY_BACKOFF_MS", "0")
    registry = getattr(per_problem_harness, "_repo_clone_locks", None)
    if isinstance(registry, dict):
        registry.clear()
    yield


def _install_fake_git(monkeypatch, *, clone_calls, fail_first_n=0, sleep_s=0.02):
    """Patch ``_run_git`` with a fake that mimics git-clone semantics.

    * Records every clone target in ``clone_calls``.
    * The first ``fail_first_n`` clone attempts simulate the production
      ``rc=128 destination not empty`` (leaving the dir partially populated,
      exactly as a real aborted clone would).
    * Otherwise yields the event loop (``sleep_s``) to widen the race window,
      then materializes a valid ``.git`` checkout.
    """
    state = {"attempt": 0}

    async def fake_run_git(args, **_kwargs):
        if not args or args[0] != "clone":
            return (0, "", "")
        from pathlib import Path

        target = Path(args[-1])
        clone_calls.append(str(target))
        state["attempt"] += 1
        if state["attempt"] <= fail_first_n:
            # simulate a half-written aborted clone: dir exists, non-empty, no .git
            target.mkdir(parents=True, exist_ok=True)
            (target / ".partial").write_text("x", encoding="utf-8")
            return (
                128,
                "",
                "fatal: destination path '%s' already exists and is "
                "not an empty directory." % target,
            )
        await asyncio.sleep(sleep_s)
        (target / ".git").mkdir(parents=True, exist_ok=True)
        (target / "README").write_text("ok", encoding="utf-8")
        return (0, "", "")

    monkeypatch.setattr(per_problem_harness, "_run_git", fake_run_git)


# --- Phase 1: per-repo serialization (the EVAL-2 race) ---

def test_concurrent_same_repo_clones_serialize_to_one(monkeypatch):
    """Two concurrent calls for the SAME repo must produce exactly ONE clone
    (the second takes the cache-hit), and both must return the same valid path."""
    clone_calls: list[str] = []
    _install_fake_git(monkeypatch, clone_calls=clone_calls)

    async def _run():
        url = "https://github.com/ansible/ansible.git"
        return await asyncio.gather(
            _ensure_repo_cached(url),
            _ensure_repo_cached(url),
        )

    a, b = asyncio.run(_run())
    assert a is not None and b is not None, "both callers must resolve a cache path"
    assert a == b, "both callers must converge on the same cache path"
    assert len(clone_calls) == 1, (
        "same-repo clones must serialize to exactly one real clone; "
        f"got {len(clone_calls)} (the EVAL-2 race)"
    )


# --- Phase 2: bounded purge-and-retry over a stale / partial dir ---

def test_rc128_stale_dir_is_purged_and_retried(monkeypatch):
    """A single clone hitting rc=128 (stale non-.git dir) must be purged and
    retried within the attempt budget, ultimately succeeding."""
    clone_calls: list[str] = []
    _install_fake_git(monkeypatch, clone_calls=clone_calls, fail_first_n=1)

    result = asyncio.run(
        _ensure_repo_cached("https://github.com/ansible/ansible.git")
    )
    assert result is not None, "purge-and-retry must recover from a stale dir"
    assert (result / ".git").is_dir(), "the retried clone must be valid"
    assert len(clone_calls) == 2, "must retry exactly once after the rc=128"


def test_retry_budget_is_bounded(monkeypatch):
    """If every attempt fails rc=128, the harness gives up (returns None) within
    the bounded attempt budget — never an unbounded loop."""
    clone_calls: list[str] = []
    _install_fake_git(monkeypatch, clone_calls=clone_calls, fail_first_n=999)

    result = asyncio.run(
        _ensure_repo_cached("https://github.com/ansible/ansible.git")
    )
    assert result is None, "exhausted retries must fail closed (None)"
    # default budget is 3 attempts — bounded, not infinite
    assert 1 < len(clone_calls) <= 5, f"retries must be bounded; got {len(clone_calls)}"


# --- Phase 3: per-repo locking does NOT over-serialize distinct repos ---

def test_different_repos_clone_concurrently(monkeypatch):
    """The lock is keyed per repo path: two DIFFERENT repos must both clone
    (no global serialization stall)."""
    clone_calls: list[str] = []
    _install_fake_git(monkeypatch, clone_calls=clone_calls)

    async def _run():
        return await asyncio.gather(
            _ensure_repo_cached("https://github.com/ansible/ansible.git"),
            _ensure_repo_cached("https://github.com/qutebrowser/qutebrowser.git"),
        )

    a, b = asyncio.run(_run())
    assert a is not None and b is not None
    assert a != b, "different repos must resolve to different cache paths"
    assert len(clone_calls) == 2, "both distinct repos must clone"


def test_warm_cache_hit_skips_clone_entirely(monkeypatch):
    """Once cached, a subsequent call takes the fast-path with zero clones —
    the lock must not perturb the existing idempotent cache-hit behavior."""
    clone_calls: list[str] = []
    _install_fake_git(monkeypatch, clone_calls=clone_calls)
    url = "https://github.com/ansible/ansible.git"

    first = asyncio.run(_ensure_repo_cached(url))
    assert first is not None and len(clone_calls) == 1
    second = asyncio.run(_ensure_repo_cached(url))
    assert second == first, "warm cache must return the same path"
    assert len(clone_calls) == 1, "warm cache-hit must not re-clone"


# --- Wiring pins: the structural fix exists in the module ---

def test_per_repo_lock_registry_exists():
    assert hasattr(per_problem_harness, "_repo_clone_locks"), (
        "Slice 76 requires a per-repo asyncio.Lock registry"
    )
    assert isinstance(per_problem_harness._repo_clone_locks, dict)


def test_ensure_repo_cached_uses_lock_and_retry():
    src = inspect.getsource(per_problem_harness._ensure_repo_cached)
    assert "_repo_clone_locks" in src or "_clone_lock_for" in src, (
        "_ensure_repo_cached must acquire the per-repo clone lock"
    )
    assert "async with" in src, "the critical section must be lock-guarded"
