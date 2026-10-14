"""
daemon_handover - a launch never attaches to a kernel running older code.

A plain ``unified_supervisor.py`` start that finds a live daemon becomes a
streaming client of it (supervisor_ipc). That is right while the daemon runs
the code in this checkout, and wrong once the checkout has moved on: after a
merge, an edit or a pull, the launch silently attached to the kernel started
before it, and the operator watched the old code believing it was the new.

So before attaching, the launch asks the daemon what it is running:

* the daemon reports its CODE IDENTITY in the supervisor socket's status
  response -- the checkout it runs from, when its process started, and the
  repo-local source files it has loaded (``code_identity``);
* a daemon from before this protocol answers status with its pid only; its
  sources are then the checkout's tracked Python files, the files such a
  daemon can have loaded;
* a source modified after the daemon started (or gone) means the daemon runs
  code this checkout no longer has. A module first imported after its file
  changed is counted too: the check errs toward the current code, never
  toward attaching to code that may be stale.

A stale daemon is HANDED OVER rather than attached to (policy
``JARVIS_STALE_DAEMON_POLICY``: ``replace``, the default, or ``attach``): it
is asked to shut down through the kernel's own command socket, its shutdown
is streamed to the terminal so the wait is visible, and the launch waits for
its process to exit. The wait has no fixed deadline: it lasts while the old
kernel is still talking, and only silence (no event for
``JARVIS_DAEMON_HANDOVER_SILENCE_S``) escalates to terminating its process
tree. ``--restart`` uses the same handover.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def stale_policy() -> str:
    policy = os.environ.get("JARVIS_STALE_DAEMON_POLICY", "").strip().lower()
    return policy if policy in ("replace", "attach") else "replace"


def _silence_s() -> float:
    return max(1.0, _env_float("JARVIS_DAEMON_HANDOVER_SILENCE_S", 30.0))


def _query_timeout_s() -> float:
    return max(0.5, _env_float("JARVIS_DAEMON_QUERY_TIMEOUT_S", 5.0))


def _process_started_at(pid: int) -> Optional[float]:
    try:
        import psutil
        return psutil.Process(pid).create_time()
    except Exception:
        return None


def _relative(path: str, root: Path) -> Optional[str]:
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return None


# -- the daemon side ------------------------------------------------------------
def loaded_sources(root: Path = REPO_ROOT) -> List[str]:
    """Repo-relative paths of the Python sources this process has loaded."""
    import sys
    seen = set()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if path and path.endswith(".py"):
            rel = _relative(path, root)
            if rel:
                seen.add(rel)
    return sorted(seen)


def code_identity(root: Path = REPO_ROOT) -> dict:
    """What this process runs: answered in the supervisor status response."""
    return {
        "root": str(root),
        "pid": os.getpid(),
        "started_at": _process_started_at(os.getpid()),
        "sources": loaded_sources(root),
    }


# -- the launcher side ----------------------------------------------------------
def _is_test_file(rel: str) -> bool:
    name = Path(rel).name
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def _never_imported_dir(name: str) -> bool:
    """Directories that are never import roots for the kernel: hidden ones
    (.git, .github, .venv ...), bytecode caches and npm trees."""
    return name.startswith(".") or name in ("__pycache__", "node_modules")


def _git_sources(root: Path) -> Optional[List[str]]:
    """Tracked .py files, or None when git cannot answer (not on PATH -- the
    usual case in a plain Windows PowerShell -- or not a checkout)."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        out = subprocess.run(
            [git, "ls-files", "-z", "--", "*.py"], cwd=str(root),
            capture_output=True, timeout=_query_timeout_s() * 4,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("[Handover] git ls-files failed: %s", exc)
        return None
    if out.returncode != 0:
        return None
    return [n for n in out.stdout.decode("utf-8", "replace").split("\0") if n]


def _walked_sources(root: Path) -> List[str]:
    """Every .py file in the checkout, without git, never entering a
    directory the kernel cannot import from (nor a virtualenv, marked by
    its pyvenv.cfg)."""
    found = []
    for here, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if not _never_imported_dir(d)
                   and not os.path.exists(os.path.join(here, d, "pyvenv.cfg"))]
        rel_here = Path(here).relative_to(root)
        found.extend((rel_here / f).as_posix() for f in files if f.endswith(".py"))
    return found


def _tracked_sources(root: Path) -> List[str]:
    """Sources a daemon without a code identity can have loaded: the
    checkout's .py files (git's list when git can answer, else the tree
    itself) that are not pytest's -- a kernel never imports its test suite."""
    names = _git_sources(root)
    if names is None:
        names = _walked_sources(root)
    return [n for n in names
            if not _is_test_file(n)
            and not any(_never_imported_dir(part) for part in Path(n).parts[:-1])]


def _query(sock_path: Path, request: dict, answer_type: Optional[str] = None) -> Optional[dict]:
    """One request over a path-addressed socket; the first matching reply.

    The supervisor stream interleaves log events with replies, so a reply is
    recognised by ``answer_type``; the kernel command socket answers once.
    """
    from backend.core.portable_ipc import connect_unix_sync
    deadline = time.monotonic() + _query_timeout_s()
    try:
        sock = connect_unix_sync(str(sock_path), timeout=_query_timeout_s())
    except (OSError, ValueError):
        return None
    try:
        sock.sendall((json.dumps(request) + "\n").encode())
        buf = b""
        while time.monotonic() < deadline:
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            chunk = sock.recv(65536)
            if not chunk:
                return None
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                if answer_type is None or reply.get("type") == answer_type:
                    return reply
    except OSError:
        return None
    finally:
        sock.close()
    return None


def _send(sock_path: Path, request: dict) -> bool:
    """Fire one request that expects no reply."""
    from backend.core.portable_ipc import connect_unix_sync
    try:
        sock = connect_unix_sync(str(sock_path), timeout=_query_timeout_s())
    except (OSError, ValueError):
        return False
    try:
        sock.sendall((json.dumps(request) + "\n").encode())
        return True
    except OSError:
        return False
    finally:
        sock.close()


@dataclass
class Staleness:
    pid: int
    started_at: float
    changed: List[str] = field(default_factory=list)
    other_root: Optional[str] = None
    what: str = "kernel"

    def describe(self, shown: int = 3) -> str:
        when = time.strftime("%H:%M:%S", time.localtime(self.started_at))
        if self.other_root:
            return f"the running {self.what} (PID {self.pid}, started {when}) runs from {self.other_root}"
        names = ", ".join(self.changed[:shown])
        more = f" and {len(self.changed) - shown} more" if len(self.changed) > shown else ""
        return (f"the running {self.what} (PID {self.pid}, started {when}) predates "
                f"{len(self.changed)} changed source file(s): {names}{more}")


def judge_staleness(pid: int, code: dict, root: Path, *, expected_root: Optional[Path] = None,
                    what: str = "kernel") -> Optional[Staleness]:
    """Is process *pid* running code the checkout *root* no longer has?

    *code* is the process's own code identity (``code_identity``: started_at,
    root, sources), possibly empty for a process from before the protocol;
    its sources are then *root*'s tracked files. None when it runs this
    checkout's code -- or cannot say when it started (then adopting it is
    the only safe answer). With *expected_root*, a process running from
    another checkout is stale too.
    """
    started = code.get("started_at") or _process_started_at(pid)
    if not started:
        return None
    if expected_root is not None and code.get("root")             and Path(code["root"]).resolve() != expected_root.resolve():
        return Staleness(pid, started, other_root=code["root"], what=what)
    sources = code.get("sources") or _tracked_sources(root)
    changed = []
    for rel in sources:
        try:
            if (root / rel).stat().st_mtime > started:
                changed.append(rel)
        except OSError:
            changed.append(rel)          # gone from the checkout
    changed.sort(key=lambda rel: _mtime(root / rel), reverse=True)
    return Staleness(pid, started, changed, what=what) if changed else None


def daemon_status(stream_sock: Path) -> Optional[dict]:
    """The live daemon's status response (pid, and its code identity when it
    is new enough to report one), or None when nothing answers."""
    return _query(stream_sock, {"cmd": "status"}, answer_type="status_response")


def running_daemon_staleness(stream_sock: Path, root: Path = REPO_ROOT) -> Optional[Staleness]:
    """Staleness of the live daemon, or None when it runs this checkout's code
    (or cannot say who it is -- then attaching is the only safe answer)."""
    status = daemon_status(stream_sock)
    if not status or not status.get("pid"):
        return None
    return judge_staleness(int(status["pid"]), status.get("code") or {}, root, expected_root=root)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return float("inf")


def _alive(pid: int) -> bool:
    try:
        import psutil
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


async def hand_over(
    pid: int,
    kernel_sock: Path,
    stream_sock: Path,
    say: Callable[[str], None] = print,
) -> bool:
    """Shut the daemon ``pid`` down and wait until it is gone. True when it is."""
    from backend.core.process_identity import kill_process_tree
    from backend.core.supervisor_ipc import SupervisorIPC

    silence = _silence_s()
    asked = await asyncio.to_thread(_query, kernel_sock, {"command": "shutdown"})
    if not (asked or {}).get("success"):
        # The command socket is not up yet (early boot) or refused: the
        # supervisor stream carries a shutdown request of its own.
        await asyncio.to_thread(_send, stream_sock, {"cmd": "shutdown"})
        say("[JARVIS] Kernel command socket did not acknowledge; asked over the stream.")

    # Stream its shutdown while it lasts; the stream going quiet is the cue.
    outcome = await SupervisorIPC().relay(stop_when=lambda: not _alive(pid), silence_s=silence)
    if outcome != "silent":
        # The stream closed while the process finishes its last cleanup:
        # one more silence window for it to exit on its own.
        deadline = time.monotonic() + silence
        while _alive(pid) and time.monotonic() < deadline:
            await asyncio.sleep(min(0.25, silence))
    if _alive(pid):
        say(f"[JARVIS] Kernel PID {pid} went silent for {silence:.0f}s without exiting "
            f"- terminating its process tree.")
        await asyncio.to_thread(kill_process_tree, pid, silence)
    return not _alive(pid)


__all__ = [
    "REPO_ROOT", "Staleness", "code_identity", "daemon_status", "hand_over", "judge_staleness",
    "loaded_sources", "running_daemon_staleness", "stale_policy",
]
