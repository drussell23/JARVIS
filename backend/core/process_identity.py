"""
process_identity - which processes are JARVIS's to signal, and are they alive.

Two hazards made the supervisor unsafe to run on Windows, and both are
answered here once, for every call site:

1. ``os.kill(pid, 0)`` is the POSIX liveness probe and appears ~100 times in
   the tree. On Windows CPython implements ``os.kill`` with
   ``TerminateProcess(handle, sig)`` for every signal except the two console
   events, so the "probe" KILLS the process (exit code 0). ``pid_alive`` is a
   real probe on every platform, and ``install_kill_guard`` reroutes signal 0
   through it, so existing ``os.kill(pid, 0)`` callers become correct without
   being rewritten.

2. Cleanup and takeover find "JARVIS processes" by regex over command lines
   ("jarvis.*backend", "jarvis.*prime", ...). On a host whose account is named
   Jarvis, every process under ``C:\\Users\\Jarvis`` matches: the browser, the
   remote-desktop client, the model server, the operator's own shell. The guard
   answers ownership from STRUCTURE instead of a substring:

   * a descendant of this process is ours;
   * an ancestor of this process is never ours (we do not kill our launcher);
   * a Python/Node interpreter is ours iff the script it runs (or, for
     ``-m pkg``, its working directory) lies inside a Trinity repo root;
   * any other executable is ours iff the executable itself lies inside a root.

   Roots are this repository plus the Prime/Reactor checkouts named by the
   environment variables the supervisor already reads, plus siblings of this
   repository that carry a ``pyproject.toml``.

The guard wraps ``os.kill`` and ``psutil.Process.{send_signal,terminate,kill}``
and refuses (``PermissionError`` / ``psutil.AccessDenied``, both of which every
caller already handles as "could not kill") with one WARNING naming the
target. It is ON by default on Windows and OFF elsewhere, where usernames do
not collide and behaviour stays byte-identical; ``JARVIS_KILL_GUARD`` set to
true/false overrides either way.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path
from contextvars import ContextVar
from typing import FrozenSet, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

#: The signal that ends a process outright. POSIX: SIGKILL. Windows has no
#: SIGKILL (referencing it raised AttributeError mid-cleanup); there
#: os.kill(pid, SIGTERM) is TerminateProcess, which is already unconditional.
HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_ROOT_ENV_VARS = (
    "JARVIS_PRIME_PATH",
    "JARVIS_PRIME_REPO_PATH",
    "REACTOR_CORE_PATH",
    "JARVIS_REACTOR_PATH",
    "JARVIS_KILL_GUARD_EXTRA_ROOTS",  # os.pathsep-separated
)

_INTERPRETER_STEMS = ("python", "pythonw", "py", "node")

_install_lock = threading.Lock()
_installed = False


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes as _ctypes
    from ctypes import wintypes as _wintypes

    _kernel32 = _ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = _wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (_wintypes.DWORD, _wintypes.BOOL, _wintypes.DWORD)
    _kernel32.GetExitCodeProcess.argtypes = (_wintypes.HANDLE, _ctypes.POINTER(_wintypes.DWORD))
    _kernel32.CloseHandle.argtypes = (_wintypes.HANDLE,)
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5

    def pid_alive(pid: Optional[int]) -> bool:
        """True iff ``pid`` names a running process. Never signals it."""
        if not pid or pid < 0:
            return False
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            # Exists but belongs to someone we may not inspect (another user,
            # an elevated or protected process).
            return _ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            code = _wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, _ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)

else:

    def pid_alive(pid: Optional[int]) -> bool:
        """True iff ``pid`` names a running process. Never signals it."""
        if not pid or pid < 0:
            return False
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True


# ---------------------------------------------------------------------------
# Identity: a PID names a process only while that process lives
# ---------------------------------------------------------------------------
# Windows recycles PIDs quickly: a kernel's lock named PID 117732, the kernel
# died, and within the hour 117732 was a VS Code GPU process -- so "is the PID
# alive" said a kernel was running and refused to boot one (2026-10-14).
# A process is identified by its PID AND its start time, recorded together.

def process_started_at(pid: Optional[int]) -> Optional[float]:
    """When the process *pid* started (the OS's own creation time, seconds
    since the epoch), or None when it is not running or cannot be read."""
    if not pid_alive(pid):
        return None
    try:
        import psutil

        return float(psutil.Process(int(pid)).create_time())
    except Exception:  # noqa: BLE001 -- gone, access denied, or no psutil
        return None


def is_same_process(pid: Optional[int], started_at: Optional[float]) -> Optional[bool]:
    """Is *pid* still the process that started at *started_at*?

    True / False when that can be told; None when it cannot (no start time
    was recorded, or this one cannot be read) -- the caller then needs other
    evidence, such as ``is_trinity_process``."""
    if not pid_alive(pid):
        return False
    if started_at is None:
        return None
    now = process_started_at(pid)
    if now is None:
        return None
    return now == float(started_at)


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------

def _norm(path: Path) -> str:
    return os.path.normcase(str(path))


def trinity_roots() -> List[Path]:
    """The directories whose code counts as Trinity's own."""
    roots: List[Path] = [_REPO_ROOT]
    for var in _ROOT_ENV_VARS:
        for raw in (os.environ.get(var) or "").split(os.pathsep):
            if raw.strip():
                roots.append(Path(raw.strip()).expanduser())
    # The sibling repositories, recognised by content (backend/core/trinity_repos);
    # "any neighbour with a pyproject.toml" also claimed unrelated projects.
    from backend.core.trinity_repos import repo_path
    roots.extend(p for p in (repo_path("prime"), repo_path("reactor")) if p is not None)
    seen: Set[str] = set()
    out: List[Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        key = _norm(resolved)
        if key not in seen and resolved.is_dir():
            seen.add(key)
            out.append(resolved)
    return out


def _within(path: Path, roots: Iterable[Path]) -> bool:
    target = _norm(path)
    for root in roots:
        base = _norm(root)
        if target == base or target.startswith(base.rstrip("\\/") + os.sep):
            return True
    return False


def _script_of(cmdline: List[str]) -> tuple:
    """(script, module) an interpreter command line runs; either may be None."""
    args = cmdline[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-m" and i + 1 < len(args):
            return None, args[i + 1]
        if arg in ("-c",):
            return None, None
        if arg in ("-X", "-W") and i + 1 < len(args):
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return arg, None
    return None, None


def _structurally_owned(proc, roots: List[Path]) -> bool:
    """An interpreter whose script (or, for -m, working directory) lies in a
    Trinity root, or any other executable that lives in one."""
    exe = proc.exe()
    stem = Path(exe).stem.lower() if exe else ""
    if not stem.startswith(_INTERPRETER_STEMS):
        return bool(exe) and _within(Path(exe), roots)
    script, module = _script_of(proc.cmdline())
    cwd = Path(proc.cwd())
    if script:
        path = Path(script)
        if not path.is_absolute():
            path = cwd / path
        return _within(path, roots)
    if module:
        return _within(cwd, roots)
    return False


def is_trinity_process(proc, roots: Optional[List[Path]] = None) -> bool:
    """Is *proc* itself running Trinity's code (no ancestry walk)?

    For sweeps that look across every process: pass ``trinity_roots()`` once.
    Command-line words cannot answer this -- "jarvis" matched every program
    on a machine whose Windows user is named Jarvis.
    """
    try:
        return _structurally_owned(proc, roots if roots is not None else trinity_roots())
    except Exception:  # noqa: BLE001 -- gone / access denied: not provably ours
        return False


def owned_by_trinity(pid: int) -> bool:
    """Structural answer to "may JARVIS signal this process?" (see module doc).

    Ownership is inherited: a process descended from a Trinity-owned one
    belongs to it (npm.cmd -> node -> react-scripts). Without that, a tree
    killed from the top lost the link mid-way -- the orphaned cmd.exe under
    the dev server was refused, and the UI's port stayed held. The walk up
    stops at this process's own lineage, so a launcher is never claimed.
    """
    if pid == os.getpid():
        return True
    try:
        import psutil
    except ImportError:
        return False
    try:
        me = os.getpid()
        roots = trinity_roots()

        def structural(p: int) -> bool:
            try:
                return _structurally_owned(psutil.Process(p), roots)
            except (psutil.Error, OSError, ValueError):
                return False

        parent = _parent_map(psutil, {p: psutil.Process(p) for p in (me, pid)})
        return _owned(pid, me, set(_ancestors(me, parent)), _ancestors(pid, parent), structural)
    except (psutil.Error, OSError, ValueError):
        return False


def _ancestors(pid: int, parent: dict) -> List[int]:
    """*pid*'s ancestors, nearest first, from a pid -> parent map."""
    chain: List[int] = []
    seen = {pid}
    nxt = parent.get(pid)
    while nxt and nxt not in seen:   # pid reuse can make a cycle
        chain.append(nxt)
        seen.add(nxt)
        nxt = parent.get(nxt)
    return chain


def _owned(pid: int, me: int, my_lineage: Set[int], ancestors: List[int],
           structural) -> bool:
    """The ownership rule, given *pid*'s ancestors (nearest first) and a
    structural test: never this process's lineage; a descendant of this
    process; structurally Trinity's; or below an ancestor that is, the walk
    stopping at this process's lineage."""
    if pid == me:
        return True
    if pid in my_lineage:
        return False
    if me in ancestors:
        return True
    if structural(pid):
        return True
    for ancestor in ancestors:
        if ancestor in my_lineage:
            return False
        if structural(ancestor):
            return True
    return False


def _parent_map(psutil, procs: dict) -> dict:
    """pid -> parent pid for the whole table, built once.

    On Windows ``Process.ppid()`` builds this very map from a full system
    snapshot (~22 ms) on every call, so asking 500 processes for their parent
    took 11 s. psutil keeps the one-shot builder ``Process.children`` uses on
    every platform; the per-process answer is the fallback where it is gone.
    """
    builder = getattr(psutil, "_ppid_map", None)
    if callable(builder):
        try:
            return dict(builder())
        except Exception:  # noqa: BLE001 -- fall through to per-process
            pass
    parent = {}
    for pid, proc in procs.items():
        try:
            parent[pid] = proc.ppid()
        except psutil.Error:
            continue
    return parent


_SNAPSHOT_LOCK = threading.Lock()
_snapshot: tuple = (0.0, frozenset())


def _snapshot_ttl_s() -> float:
    try:
        return max(0.0, float(os.environ.get("JARVIS_OWNERSHIP_SNAPSHOT_TTL_S", "") or 2.0))
    except ValueError:
        return 2.0


def trinity_owned_pids() -> FrozenSet[int]:
    """Every PID :func:`owned_by_trinity` would accept, from ONE pass over the
    process table -- for sweeps that ask about every process. Asked process by
    process, each answer re-walked the whole table (minutes of CPU for one
    boot sweep). Shared for ``JARVIS_OWNERSHIP_SNAPSHOT_TTL_S`` so the sweeps
    of one cleanup reuse it; a process newer than the snapshot is simply not
    in it, which only ever spares it."""
    global _snapshot
    import time as _time

    with _SNAPSHOT_LOCK:
        taken, owned = _snapshot
        if _time.monotonic() - taken < _snapshot_ttl_s():
            return owned
        try:
            import psutil
        except ImportError:
            return frozenset()
        procs = {proc.pid: proc for proc in psutil.process_iter()}
        parent = _parent_map(psutil, procs)
        roots = trinity_roots()
        verdicts: dict = {}

        def structural(p: int) -> bool:
            if p not in verdicts:
                proc = procs.get(p)
                try:
                    verdicts[p] = proc is not None and _structurally_owned(proc, roots)
                except (psutil.Error, OSError, ValueError):
                    verdicts[p] = False
            return verdicts[p]

        me = os.getpid()
        lineage = set(_ancestors(me, parent))
        owned = frozenset(p for p in procs
                          if _owned(p, me, lineage, _ancestors(p, parent), structural))
        _snapshot = (_time.monotonic(), owned)
        return owned


def _describe(pid: int) -> str:
    try:
        import psutil

        proc = psutil.Process(pid)
        return f"pid={pid} name={proc.name()!r}"
    except Exception:  # noqa: BLE001
        return f"pid={pid}"


def descends_from(pid: Optional[int], ancestor: Optional[int], max_depth: int = 16) -> bool:
    """True iff *pid* is *ancestor* or was started, directly or not, by it.

    Walks parent pid NUMBERS, so a child whose launcher has already exited
    still counts as that launcher's (a venv's python.exe on Windows is a
    launcher, and the server it runs is its child). A link is only followed
    to a parent that is older than its child, so a recycled pid is not an
    ancestor.
    """
    if not pid or not ancestor:
        return False
    if pid == ancestor:
        return True
    try:
        import psutil
    except ImportError:
        return False
    try:
        proc = psutil.Process(pid)
        for _ in range(max_depth):
            parent_pid = proc.ppid()
            if not parent_pid or parent_pid == proc.pid:
                return False
            if parent_pid == ancestor:
                try:
                    return psutil.Process(parent_pid).create_time() <= proc.create_time()
                except psutil.NoSuchProcess:
                    return True  # the launcher has exited; its child is still its own
            parent = psutil.Process(parent_pid)
            if parent.create_time() > proc.create_time():
                return False
            proc = parent
    except (psutil.Error, OSError):
        return False
    return False


#: Pids a kill_process_tree call has authorized as one unit (see below).
_authorized_tree: ContextVar[FrozenSet[int]] = ContextVar("jarvis_authorized_tree", default=frozenset())


def kill_process_tree(pid: int, timeout: float = 5.0) -> bool:
    """Terminate *pid* and every descendant: deepest first, then force-kill
    survivors after *timeout*. True if the tree is gone.

    Where there are no process groups (Windows), this is the only way to stop
    what a launcher started: terminating ``npm.cmd`` left its ``node``
    dev server holding the UI's port after JARVIS had stopped.

    Ownership is judged ONCE, on the intact tree, at its root: a tree falls
    apart while it is being killed (npm's parents exit as soon as their child
    does), so re-judging each member mid-kill refused live, now-orphaned
    members. When the guard is active and the root is not Trinity's, nothing
    is touched.
    """
    try:
        import psutil
    except ImportError:
        return False
    try:
        parent = psutil.Process(pid)
        if kill_guard_enabled() and not owned_by_trinity(pid):
            logger.warning("[KillGuard] refused to stop the tree of %s: not Trinity-owned",
                           _describe(pid))
            return False
        children = parent.children(recursive=True)
        token = _authorized_tree.set(frozenset([pid] + [c.pid for c in children]))
        try:
            # Deepest first: a child never outlives the parent that would reap it.
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            try:
                parent.terminate()
            except psutil.NoSuchProcess:
                pass
            gone, alive = psutil.wait_procs([parent] + children, timeout=timeout)
            for proc in alive:
                try:
                    logger.warning("Force killing stuck process PID %s", proc.pid)
                    proc.kill()
                except psutil.NoSuchProcess:
                    pass
            psutil.wait_procs(alive, timeout=2.0)
        finally:
            _authorized_tree.reset(token)
        return True
    except psutil.NoSuchProcess:
        return True  # Already gone
    except Exception as exc:  # noqa: BLE001
        logger.error("Error killing process tree %s: %s", pid, exc)
        return False


def _may_signal(pid: int) -> bool:
    """The kill guard's one rule for both os.kill and psutil.Process.

    A member of a tree being stopped as a unit; a process that no longer
    exists (the real call reports that, the guard must not call it foreign);
    otherwise structural ownership.
    """
    if pid in _authorized_tree.get():
        return True
    if not pid_alive(pid):
        return True
    return owned_by_trinity(pid)


def may_signal(pid: int) -> bool:
    """Would the kill guard let JARVIS signal *pid*? For sweeps choosing what
    to stop: a sweep that selects by command-line words and lets the guard
    refuse at the last step picked the operator's own shells (their command
    lines mentioned "jarvis") and logged a refusal for each at every boot.
    Answered from :func:`trinity_owned_pids`, so a sweep costs one pass over
    the process table. Always True where the guard is off."""
    if not kill_guard_enabled() or pid in _authorized_tree.get():
        return True
    return pid in trinity_owned_pids()


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

def kill_guard_enabled() -> bool:
    raw = (os.environ.get("JARVIS_KILL_GUARD") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return sys.platform == "win32"


def install_kill_guard() -> bool:
    """Idempotently wrap the process-signalling primitives. Returns active?"""
    global _installed
    with _install_lock:
        if _installed:
            return True
        if not kill_guard_enabled():
            return False

        real_kill = os.kill

        def guarded_kill(pid: int, sig: int) -> None:
            if sig == 0:
                if sys.platform == "win32":
                    if not pid_alive(pid):
                        raise ProcessLookupError(3, "No such process")
                    return None
                return real_kill(pid, sig)
            if not _may_signal(pid):
                logger.warning(
                    "[KillGuard] refused signal %s to %s: not a Trinity-owned process",
                    sig, _describe(pid),
                )
                raise PermissionError(1, "Operation not permitted (JARVIS kill guard)")
            return real_kill(pid, sig)

        guarded_kill.__wrapped__ = real_kill  # type: ignore[attr-defined]
        os.kill = guarded_kill  # type: ignore[assignment]

        try:
            import psutil

            def _wrap(method_name: str) -> None:
                real = getattr(psutil.Process, method_name)

                def guarded(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                    if not _may_signal(self.pid):
                        logger.warning(
                            "[KillGuard] refused %s on %s: not a Trinity-owned process",
                            method_name, _describe(self.pid),
                        )
                        raise psutil.AccessDenied(self.pid, msg="JARVIS kill guard")
                    return real(self, *args, **kwargs)

                guarded.__wrapped__ = real  # type: ignore[attr-defined]
                setattr(psutil.Process, method_name, guarded)

            for name in ("send_signal", "terminate", "kill"):
                _wrap(name)
        except ImportError:
            pass

        _installed = True
        logger.info("[KillGuard] active (roots: %s)", ", ".join(str(r) for r in trinity_roots()))
        return True


__all__ = [
    "HARD_KILL_SIGNAL",
    "install_kill_guard",
    "is_same_process",
    "is_trinity_process",
    "kill_process_tree",
    "kill_guard_enabled",
    "may_signal",
    "owned_by_trinity",
    "trinity_owned_pids",
    "pid_alive",
    "process_started_at",
    "trinity_roots",
]
