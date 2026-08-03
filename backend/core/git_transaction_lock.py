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
In-process this is a **cooperative advisory** lock, in the same family as
``flock``: it constrains processes that call it. Enforcement against
non-participants comes from the native git hooks installed by
``scripts/install_hooks.py``, which git invokes regardless of caller and which
consult :func:`probe_lock`.

What the hooks DO enforce — every operation that moves a ref, via
``reference-transaction`` (commit, reset that moves HEAD, checkout/switch,
merge, cherry-pick, fetch, branch) plus ``pre-rebase`` and ``pre-push``.

What NOTHING can enforce — git exposes **no pre-hook for index or working-tree
mutation that does not move a ref**. So these remain unguarded, and claiming
otherwise would be false:

  * ``git reset --hard <current-HEAD>``  (exactly the 2026-08-02 shape: wipes
    the index and worktree while the ref stays put, so no ref transaction opens)
  * ``git checkout -- .`` / ``git restore .``
  * ``git stash`` (stash does move a ref, so it IS covered; listed here only to
    note the boundary is "ref moved?", not "dangerous?")

The hook therefore raises the floor from "cooperating agents only" to "any
process that moves a ref", which covers the hijack sequence observed
(checkout → commit → checkout → pull), but not a bare same-commit hard reset.
For that class the durable mitigations remain: commit early, and use a real
worktree (separate index + HEAD).

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
import json
import logging
import os
import shlex
import subprocess
import weakref
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
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
    "LockProbe",
    "probe_lock",
    "TOKEN_ENV",
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
        "Advisory in-process; enforced for ref-moving operations by the native "
        "git hooks installed via scripts/install_hooks.py. Pure index/worktree "
        "clobbers (reset --hard to the same commit, checkout -- .) have no "
        "native pre-hook in git and remain unenforceable."
    )


# ---------------------------------------------------------------------------
# Read-only probe — used by the native git hooks, which must NEVER acquire
# ---------------------------------------------------------------------------

#: Set in the environment for the duration of a held transaction, so a git
#: subprocess spawned inside the critical section (which inherits the env) is
#: recognised as the lock OWNER by the hook and allowed through. Without this
#: the holder's own commit would be blocked by its own lock.
TOKEN_ENV = "JARVIS_GIT_TXN_TOKEN"


@dataclass(frozen=True)
class LockProbe:
    """Outcome of a read-only inspection of the git mutex."""

    held_by_other: bool
    owner: Optional[str] = None
    token: Optional[str] = None
    reason: str = "free"

    def as_dict(self) -> dict:
        return {
            "held_by_other": self.held_by_other,
            "owner": self.owner,
            "token": self.token,
            "reason": self.reason,
        }


def _read_lock_file(path: Path) -> Optional[dict]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def probe_lock(cwd: Optional[Path] = None) -> LockProbe:
    """Inspect the git mutex WITHOUT acquiring it.

    Synchronous and event-loop free: a git hook runs in a fresh short-lived
    process on every ref transaction, so spinning up asyncio and a full
    DistributedLockManager there would add latency to every git command in the
    repository.

    Liveness and expiry are delegated to DLM's own primitives
    (``LockMetadata.is_expired`` and ``_is_process_alive_sync``, which carries
    the PID-reuse detection), so a dead or expired holder never blocks — the
    semantics stay identical to the acquire path rather than being restated.
    """
    data = _read_lock_file(git_lock_path(cwd))
    if data is None:
        return LockProbe(False, reason="no_lock_file")

    owner = data.get("owner")
    token = data.get("token")

    # Our own transaction (or a git child spawned inside it) — always allowed.
    env_token = os.getenv(TOKEN_ENV)
    if env_token and token and env_token == token:
        return LockProbe(False, owner=owner, token=token, reason="self")

    try:
        from backend.core.distributed_lock_manager import (
            DistributedLockManager,
            LockMetadata,
        )

        meta = LockMetadata(**{
            k: v for k, v in data.items()
            if k in LockMetadata.__dataclass_fields__  # type: ignore[attr-defined]
        })
        if meta.is_expired():
            return LockProbe(False, owner=owner, token=token, reason="expired")
        alive = DistributedLockManager._is_process_alive_sync(str(owner or ""), data)
    except Exception as exc:  # noqa: BLE001 — probe must never crash a git command
        logger.debug("[GitMutex] probe degraded (%s); treating lock as live", exc)
        alive = True

    if not alive:
        return LockProbe(False, owner=owner, token=token, reason="owner_dead")
    return LockProbe(True, owner=owner, token=token, reason="held")


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


# ---------------------------------------------------------------------------
# Atomic test-and-set gate
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS — DistributedLockManager is not mutually exclusive.
#
# Measured 2026-08-02: six concurrent tasks against a single DLM lock name
# admitted FOUR simultaneous holders. The acquire path reads the lock file, then
# writes it via `tmp + os.replace`. The write is atomic but `replace` OVERWRITES
# — it does not fail when the file appeared in between — so there is no
# test-and-set anywhere. The per-lock `asyncio.Semaphore` (dlm:843) is taken
# only inside `_write_lock_metadata` (dlm:1422); its docstring is explicit that
# it serializes *writes to the lock file*, which protects file integrity, not
# ownership. Two racers therefore both observe "free" and both proceed.
#
# That is a defect in the shared primitive and affects its other consumers
# (vbia_events, heartbeat, cross-repo state). Fixing DLM itself is the true root
# fix but has repo-wide blast radius, so it is reported rather than performed
# here. This gate supplies exactly the missing property — O_CREAT|O_EXCL, where
# the kernel picks a single winner — and DLM is still composed underneath for
# metadata, TTL/keepalive, telemetry and dead-owner semantics.
#
# Stale gates are reclaimed using DLM's OWN liveness check, so the notion of "a
# dead owner" stays defined in one place.

_GATE_SUFFIX = ".gate"


def _gate_path(cwd: Optional[Path] = None) -> Path:
    return _lock_dir(cwd) / f"{LOCK_NAME}{_GATE_SUFFIX}"


def _gate_owner_alive(path: Path) -> bool:
    """True if the process recorded in the gate file is still running."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # Unreadable or half-written: treat as dead so a crash cannot wedge
        # the repository forever.
        return False
    owner = str(payload.get("owner", ""))
    if not owner:
        return False
    try:
        from backend.core.distributed_lock_manager import DistributedLockManager

        return bool(DistributedLockManager._is_process_alive_sync(owner, payload))
    except Exception:  # noqa: BLE001 — conservative: assume alive
        return True


def _try_create_gate(path: Path, owner: str) -> bool:
    """Single atomic attempt. Returns True iff THIS caller created the gate."""
    payload = json.dumps(
        {"owner": owner, "pid": os.getpid(), "process_start_time": 0.0},
        sort_keys=True,
    )
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    return True


async def _acquire_exclusive_gate(
    cwd: Optional[Path], *, timeout: float, operation: str
) -> Optional[Path]:
    """Block until this caller exclusively owns the gate, or raise."""
    path = _gate_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = f"jarvis-{os.getpid()}-0.0"

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    delay = 0.01
    while True:
        if _try_create_gate(path, owner):
            return path
        if not _gate_owner_alive(path):
            # Owner died holding it — reclaim, then re-race honestly.
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if loop.time() >= deadline:
            raise GitTransactionBusy(
                f"git transaction lock held by another agent; "
                f"waited {timeout:.1f}s for operation: {operation}"
            )
        await asyncio.sleep(delay)
        delay = min(delay * 2, 0.25)


def _release_exclusive_gate(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


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

    # --- atomic test-and-set gate (see _acquire_exclusive_gate) --------------
    # DLM alone is NOT mutually exclusive; this gate is what makes the mutex a
    # mutex. Taken before DLM so the winner is decided by the kernel.
    gate = await _acquire_exclusive_gate(cwd, timeout=timeout, operation=operation)

    # The DLM `acquire` context manager traps exceptions raised inside its body and
    # reports them as "Lock acquire delegation failed", which both loses the caller's
    # exception and leaves its generator un-stopped (RuntimeError: generator didn't
    # stop after athrow()). So the lock is entered and exited EXPLICITLY: the caller's
    # exception propagates in this frame and is never thrown into DLM's generator.
    stack = AsyncExitStack()
    try:
        acquired = await stack.enter_async_context(
            manager.acquire(LOCK_NAME, timeout=timeout, ttl=ttl, enable_keepalive=True)
        )
    except BaseException:
        # The gate is already ours; never leave it behind on a failed acquire or
        # it would wedge the repository until the owner PID died.
        _release_exclusive_gate(gate)
        raise
    if not acquired:
        await stack.aclose()
        _release_exclusive_gate(gate)
        raise GitTransactionBusy(
            f"git transaction lock held by another agent; "
            f"waited {timeout:.1f}s for operation: {operation}"
        )

    waited = asyncio.get_running_loop().time() - waited_from
    if waited > 0.5:
        logger.info("[GitMutex] acquired after %.2fs queue wait: %s", waited, operation)
    else:
        logger.debug("[GitMutex] acquired: %s", operation)

    # Publish our ownership token so the native hooks let OUR git children
    # through. Read back from the lock file rather than guessed: DLM mints the
    # token internally, and the hook compares against exactly that value.
    meta = _read_lock_file(git_lock_path(cwd))
    token = (meta or {}).get("token")
    had_token = TOKEN_ENV in os.environ
    prev_token = os.environ.get(TOKEN_ENV)
    if token:
        os.environ[TOKEN_ENV] = str(token)

    try:
        yield
    finally:
        if token:
            if had_token:
                os.environ[TOKEN_ENV] = prev_token or ""
            else:
                os.environ.pop(TOKEN_ENV, None)
        await stack.aclose()
        _release_exclusive_gate(gate)
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
