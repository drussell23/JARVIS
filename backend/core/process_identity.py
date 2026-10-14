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
import sys
import threading
from pathlib import Path
from contextvars import ContextVar
from typing import FrozenSet, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

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
    try:
        for sibling in _REPO_ROOT.parent.iterdir():
            if sibling != _REPO_ROOT and (sibling / "pyproject.toml").is_file():
                roots.append(sibling)
    except OSError:
        pass
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
        me = psutil.Process(os.getpid())
        my_lineage = {p.pid for p in me.parents()}
        if pid in my_lineage:
            return False
        if any(c.pid == pid for c in me.children(recursive=True)):
            return True
        proc = psutil.Process(pid)
        roots = trinity_roots()
        if _structurally_owned(proc, roots):
            return True
        for ancestor in proc.parents():
            if ancestor.pid in my_lineage:
                return False
            if ancestor.pid == me.pid:
                return True
            try:
                if _structurally_owned(ancestor, roots):
                    return True
            except (psutil.Error, OSError, ValueError):
                continue
        return False
    except (psutil.Error, OSError, ValueError):
        return False


def _describe(pid: int) -> str:
    try:
        import psutil

        proc = psutil.Process(pid)
        return f"pid={pid} name={proc.name()!r}"
    except Exception:  # noqa: BLE001
        return f"pid={pid}"


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
    "install_kill_guard",
    "is_trinity_process",
    "kill_process_tree",
    "kill_guard_enabled",
    "owned_by_trinity",
    "pid_alive",
    "trinity_roots",
]
