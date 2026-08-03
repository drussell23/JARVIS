"""
Autonomic Git-Mutex — cross-process transaction lock for state-mutating git operations.

Why this exists
---------------
On 2026-08-02 a concurrent autonomous process performed, inside this repository,
while another agent held 201 staged renames in the index::

    checkout: chore/organize-root-docs -> fix/speech-gate-mic-suppression
    commit:   fix(voice): one arbiter for "JARVIS is speaking"
    checkout: fix/speech-gate-mic-suppression -> main
    pull --ff-only origin main: Fast-forward
    reset:    moving to origin/main

The staged work survived only because the trailing reset happened to be a tree
no-op. A ``git reset --hard`` would have destroyed it. Branch isolation offers
no protection here: in a single worktree the index and working tree are shared
state, and ``git checkout -b`` does not fence them. The only real remedy is
mutual exclusion around the mutating operation itself.

Contract
--------
Any autonomous actor (Venom tool loop, HUD, background script, soak harness)
MUST wrap a state-mutating git invocation in :func:`git_transaction`, or issue
it through :func:`run_git`, which classifies and wraps automatically.

Read-only plumbing (``status``, ``log``, ``diff``, ``rev-parse``, ...) is not
serialized — it neither corrupts nor observes a torn index in a way that
matters here, and locking it would deadlock any nested inspection.

Scope and honest limits
-----------------------
This is a **cooperative advisory** lock, in the same family as ``flock``. It
constrains processes that call it. A human typing ``git reset --hard`` in a
terminal, or a third-party tool that does not participate, is NOT stopped by
this module. Enforcement against non-participants requires a git hook
(``reference-transaction`` / ``pre-commit``), which git itself invokes
regardless of caller; see :func:`advisory_scope_note`.

DRY
---
The locking primitive is NOT reimplemented here. This module composes
``backend.core.distributed_lock_manager.DistributedLockManager``, the same
cross-process manager used for ``vbia_events`` and ``heartbeat``. Note that
DLM's file backend is atomic-create + JSON metadata with dead-owner detection
(``*.dlm.lock``), not ``fcntl.flock``; it deliberately uses a distinct
extension so it cannot collide with flock-based locks sharing a directory.

Environment
-----------
``JARVIS_GIT_MUTEX_ENABLED``     master switch (default ``true``)
``JARVIS_GIT_MUTEX_TIMEOUT_S``   max wait to acquire (default ``120``)
``JARVIS_GIT_MUTEX_TTL_S``       lock TTL, keepalive-extended (default ``300``)
``JARVIS_GIT_MUTEX_FAIL_OPEN``   if ``true``, proceed unlocked when the lock
                                 subsystem is unavailable (default ``false`` —
                                 fail closed, per Manifesto §1 Boundary)
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import weakref
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator, Iterable, Optional, Sequence

if TYPE_CHECKING:  # import cost stays off the hot path
    from backend.core.distributed_lock_manager import DistributedLockManager

logger = logging.getLogger(__name__)

__all__ = [
    "GitTransactionBusy",
    "GitMutexUnavailable",
    "MUTATING_GIT_SUBCOMMANDS",
    "classify_git_argv",
    "is_mutating_git_argv",
    "git_transaction",
    "run_git",
    "git_lock_path",
    "advisory_scope_note",
]

LOCK_NAME = "git_transaction"

#: Subcommands that mutate HEAD, the index, the working tree, or refs.
#: Anything here must be serialized. Conservative by intent: a false positive
#: costs a few milliseconds of waiting; a false negative costs an index.
MUTATING_GIT_SUBCOMMANDS = frozenset(
    {
        "add", "am", "apply", "branch", "checkout", "cherry-pick", "clean",
        "commit", "filter-branch", "fetch", "gc", "merge", "mv", "prune",
        "pull", "push", "rebase", "reset", "restore", "revert", "rm",
        "stash", "submodule", "switch", "tag", "worktree",
    }
)

#: Global flags that take a value and therefore consume the following argv slot
#: while scanning for the subcommand (``git -C <path> reset`` etc.).
_VALUE_TAKING_GLOBAL_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})


class GitTransactionBusy(RuntimeError):
    """Another agent holds the git transaction lock and it did not free in time."""


class GitMutexUnavailable(RuntimeError):
    """The lock subsystem could not be reached and fail-open is disabled."""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def classify_git_argv(argv: Sequence[str]) -> Optional[str]:
    """Return the git subcommand in ``argv``, skipping global flags.

    ``argv`` may or may not include a leading ``git``. Returns ``None`` when no
    subcommand can be identified (e.g. ``git --version``).
    """
    items = list(argv)
    if items and Path(items[0]).name in ("git", "git.exe"):
        items = items[1:]

    i = 0
    while i < len(items):
        tok = items[i]
        if tok in _VALUE_TAKING_GLOBAL_FLAGS:
            i += 2
            continue
        if tok.startswith("-"):
            # `--git-dir=x` style, or a valueless global flag such as --no-pager
            i += 1
            continue
        return tok
    return None


def is_mutating_git_argv(argv: Sequence[str]) -> bool:
    """True when ``argv`` names a git subcommand that mutates repository state."""
    sub = classify_git_argv(argv)
    return sub is not None and sub in MUTATING_GIT_SUBCOMMANDS


def _repo_root(cwd: Optional[Path] = None) -> Path:
    """Resolve the top level of the working tree containing ``cwd``."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd or os.getcwd()).resolve()


def _git_common_dir(cwd: Optional[Path] = None) -> Optional[Path]:
    """Resolve ``$GIT_COMMON_DIR`` — the ``.git`` shared by all linked worktrees."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _lock_dir(cwd: Optional[Path] = None) -> Path:
    """Lock directory, scoped to the repository being mutated.

    Lives under ``$GIT_COMMON_DIR`` (``.git/ouroboros-locks``), NOT under the
    working tree, for two load-bearing reasons:

    1. **A lock inside the work tree gets committed.** The first draft placed it
       at ``<repo>/.ouroboros/locks``; a concurrent ``git add -A`` promptly staged
       ``git_transaction.dlm.lock`` and ``.fencing_counter`` alongside the caller's
       work (caught by ``test_index_survives_concurrent_stage_and_reset``, which
       counted 42 staged paths where 40 were expected). A mutex must not pollute
       the index it exists to protect. ``$GIT_DIR`` is never tracked.

    2. **Linked worktrees must share it.** ``checkout``/``reset``/``merge`` move
       refs, and refs live in the common dir shared by every linked worktree. Two
       worktrees of one repository therefore *must* serialize; ``--git-common-dir``
       gives that, while a per-worktree path would not. Distinct repositories still
       resolve to distinct dirs and never contend.

    Falls back to the working-tree path only when git cannot answer (bare/absent).
    """
    common = _git_common_dir(cwd)
    if common is not None:
        return common / "ouroboros-locks"
    return _repo_root(cwd) / ".ouroboros" / "locks"


def git_lock_path(cwd: Optional[Path] = None) -> Path:
    """Concrete on-disk path of the mutex file (for diagnostics and tests)."""
    return _lock_dir(cwd) / f"{LOCK_NAME}.dlm.lock"


def advisory_scope_note() -> str:
    """One-line statement of what this lock does and does not constrain."""
    return (
        "Advisory: serializes participating processes only. A human shell or a "
        "non-participating tool can still mutate the index; enforce those with a "
        "git reference-transaction hook."
    )


#: One DistributedLockManager per repository lock directory.
#:
#: Deliberately NOT ``distributed_lock_manager.get_lock_manager()``: that accessor
#: is a process-wide singleton stashed on ``sys``, and it returns any pre-existing
#: instance *while discarding the config passed in*. A git mutex must be scoped to
#: the repository it guards, so routing through the singleton would silently bind
#: this lock to whatever lock_dir the first unrelated caller happened to install.
#: Caching per lock_dir keeps one instance (and one in-process semaphore) per repo,
#: which is what makes same-process queuing work, while still reusing DLM as the
#: only locking primitive.
#: {event_loop: {lock_dir: manager}}.
#:
#: Keyed by loop as well as directory because a DistributedLockManager owns
#: asyncio primitives (semaphores, an init lock) that bind to the loop that
#: created them. Reusing one across loops raises "is bound to a different event
#: loop" — and JARVIS legitimately runs several (supervisor, battle harness,
#: per-test loops). A WeakKeyDictionary drops each loop's managers when the loop
#: is collected, so this cannot leak or hand back a manager on a dead loop.
_managers: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Path, DistributedLockManager]]" = (
    weakref.WeakKeyDictionary()
)
_guards: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _loop_guard() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    guard = _guards.get(loop)
    if guard is None:
        guard = asyncio.Lock()
        _guards[loop] = guard
    return guard


async def _get_manager(cwd: Optional[Path]):
    """Return the DLM bound to this repo's lock directory. Never invents a primitive."""
    from backend.core.distributed_lock_manager import DistributedLockManager, LockConfig

    loop = asyncio.get_running_loop()
    lock_dir = _lock_dir(cwd).resolve()

    by_dir = _managers.get(loop)
    if by_dir is not None and lock_dir in by_dir:
        return by_dir[lock_dir]

    async with _loop_guard():
        by_dir = _managers.setdefault(loop, {})
        cached = by_dir.get(lock_dir)
        if cached is not None:
            return cached
        lock_dir.mkdir(parents=True, exist_ok=True)
        manager = DistributedLockManager(
            LockConfig(
                lock_dir=lock_dir,
                fencing_counter_file=lock_dir / ".fencing_counter",
            )
        )
        await manager.initialize()
        by_dir[lock_dir] = manager
        return manager


@asynccontextmanager
async def git_transaction(
    operation: str,
    *,
    cwd: Optional[Path] = None,
    timeout: Optional[float] = None,
    ttl: Optional[float] = None,
) -> AsyncIterator[None]:
    """Serialize a state-mutating git operation across processes.

    Args:
        operation: human-readable description, recorded for observability.
        cwd: repository to scope the lock to (defaults to the process cwd).
        timeout: max seconds to wait for the lock before raising.
        ttl: lock lifetime; keepalive extends it while the body runs.

    Raises:
        GitTransactionBusy: the lock did not become free within ``timeout``.
        GitMutexUnavailable: the lock subsystem failed and fail-open is off.

    Fails closed by design (Manifesto §1 Boundary): if exclusion was promised
    and cannot be delivered, the caller does not silently proceed to stomp a
    shared index.
    """
    if not _env_bool("JARVIS_GIT_MUTEX_ENABLED", True):
        logger.debug("[GitMutex] disabled by flag; proceeding unlocked: %s", operation)
        yield
        return

    timeout = timeout if timeout is not None else _env_float("JARVIS_GIT_MUTEX_TIMEOUT_S", 120.0)
    ttl = ttl if ttl is not None else _env_float("JARVIS_GIT_MUTEX_TTL_S", 300.0)

    try:
        manager = await _get_manager(cwd)
    except Exception as exc:  # lock subsystem unreachable
        if _env_bool("JARVIS_GIT_MUTEX_FAIL_OPEN", False):
            logger.warning("[GitMutex] unavailable (%s); fail-open, proceeding: %s", exc, operation)
            yield
            return
        raise GitMutexUnavailable(
            f"git mutex unavailable ({exc}); refusing to mutate git state unlocked. "
            "Set JARVIS_GIT_MUTEX_FAIL_OPEN=true to override."
        ) from exc

    waited_from = asyncio.get_running_loop().time()

    # The DLM `acquire` context manager traps exceptions raised inside its body and
    # reports them as "Lock acquire delegation failed", which both loses the caller's
    # exception and leaves its generator un-stopped (RuntimeError: generator didn't
    # stop after athrow()). So the lock is entered and exited EXPLICITLY: the caller's
    # exception propagates in this frame and is never thrown into DLM's generator.
    stack = AsyncExitStack()
    acquired = await stack.enter_async_context(
        manager.acquire(LOCK_NAME, timeout=timeout, ttl=ttl, enable_keepalive=True)
    )
    if not acquired:
        await stack.aclose()
        raise GitTransactionBusy(
            f"git transaction lock held by another agent; "
            f"waited {timeout:.1f}s for operation: {operation}"
        )

    waited = asyncio.get_running_loop().time() - waited_from
    if waited > 0.5:
        logger.info("[GitMutex] acquired after %.2fs queue wait: %s", waited, operation)
    else:
        logger.debug("[GitMutex] acquired: %s", operation)
    try:
        yield
    finally:
        await stack.aclose()
        logger.debug("[GitMutex] released: %s", operation)


async def run_git(
    args: Iterable[str],
    *,
    cwd: Optional[Path] = None,
    timeout: Optional[float] = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Run a git command, serializing it when it mutates repository state.

    Read-only subcommands bypass the mutex. Mutating subcommands acquire it and
    hold it for the duration of the subprocess.
    """
    argv = list(args)
    if not argv or Path(argv[0]).name not in ("git", "git.exe"):
        argv = ["git", *argv]

    async def _spawn() -> subprocess.CompletedProcess:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        result = subprocess.CompletedProcess(
            argv, proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, argv, result.stdout, result.stderr)
        return result

    if not is_mutating_git_argv(argv):
        return await _spawn()

    async with git_transaction(shlex.join(argv), cwd=cwd):
        return await _spawn()
