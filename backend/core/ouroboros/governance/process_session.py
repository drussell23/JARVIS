"""Process-session ownership — a spawned tool's descendants die with it.

## What this exists for

On 2026-09-20 a Sentinel soak asked the model to test a 43-line launcher
script. Importing it started a JARVIS backend on port 8010 and then ran
``tail -f``. ``pytest-timeout`` hard-exited pytest at the per-test cap — and
the server and the ``tail`` lived on, re-parented to init. Thirty-five minutes
later the host carried one rogue backend server and fifteen orphaned tails,
two or three more per retry.

Every spawn site had a partial idea of cleanup, and the partial ideas shared a
blind spot:

  * ``test_subprocess_helper`` isolates a session and ``killpg``s it — on
    timeout, cancellation and drain failure. Not when pytest exits BY ITSELF,
    which is exactly what a ``pytest-timeout`` kill looks like from outside.
  * ``BackgroundMonitor`` (the streaming path production actually uses) never
    isolated a session at all, and only ever signalled the leader.
  * ``TestRunner``'s legacy path: same.

The rule that was missing is about OWNERSHIP, not about timeouts: **whoever
creates a session reaps it when the run ends, however the run ended.** A
leader that exited cleanly can still have left a group behind.

## Why a reap is not just ``os.killpg``

Once the leader is gone and its group is EMPTY, the kernel may recycle that
pid — and ``killpg`` on a recycled id signals a stranger's process group. So
the group is enumerated first (``/proc``, matching BOTH process-group and
session id against the leader's pid — the pair ``start_new_session`` creates),
and a signal is sent only when survivors are actually present. A group that
still has members cannot have its id recycled, so a non-empty enumeration is
also the proof that the signal is aimed at the right group.

Where ``/proc`` is unavailable the enumeration is unknowable, and the reap
falls back to signalling only while the leader is still alive — the one case
in which the id is certainly still ours.

A descendant that calls ``setsid`` itself leaves the session and is beyond
this primitive; containing that takes a cgroup, not a signal. NEVER raises.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_PROC = Path("/proc")


def _stat_ids(pid_dir: Path) -> Optional[Tuple[int, int, str]]:
    """``(pgrp, session, state)`` from ``/proc/<pid>/stat``, or ``None``.

    The command name (field 2) is parenthesised and may itself contain spaces
    and parentheses, so fields are counted from the LAST ``)``.
    """
    try:
        raw = (pid_dir / "stat").read_text(errors="replace")
        tail = raw[raw.rindex(")") + 2:].split()
        # after the name: state ppid pgrp session ...
        return int(tail[2]), int(tail[3]), tail[0]
    except (OSError, ValueError, IndexError):
        return None


def session_survivors(leader_pid: int) -> Optional[Tuple[int, ...]]:
    """Live members of the session *leader_pid* created, excluding the leader.

    ``None`` means "cannot tell" (no ``/proc``) — distinct from ``()``, which
    is a positive statement that nobody is left. Zombies are not survivors:
    they hold no resources and are not ours to reap.
    """
    try:
        if leader_pid <= 1 or not _PROC.is_dir():
            return None
        found = []
        for entry in _PROC.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == leader_pid:
                continue
            ids = _stat_ids(entry)
            if ids is None:
                continue
            pgrp, session, state = ids
            if pgrp == leader_pid and session == leader_pid and state != "Z":
                found.append(pid)
        return tuple(sorted(found))
    except OSError:
        return None


def _leader_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def reap_session(leader_pid: int, *, owner: str = "") -> Tuple[int, ...]:
    """SIGKILL whatever is left of the session *leader_pid* leads.

    Call it when a run ENDS — every ending, not only the unhappy ones. Returns
    the survivors that were present (``()`` when the group was already empty),
    so a caller can report a leak instead of silently absorbing it.
    """
    try:
        pid = int(leader_pid)
        if pid <= 1:
            return ()
        try:
            if pid == os.getpgrp():
                # Not a session we created: the child shares OUR group, and
                # signalling it would be suicide.
                return ()
        except OSError:
            return ()

        survivors = session_survivors(pid)
        if survivors is None:
            # Cannot enumerate. The id is provably ours only while the leader
            # still holds it.
            if not _leader_alive(pid):
                return ()
            survivors = ()
        elif not survivors and not _leader_alive(pid):
            return ()  # empty group: nothing to do, and the id may be recycled

        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if survivors:
            logger.warning(
                "[ProcessSession] %s left %d descendant(s) behind (pids=%s) — "
                "reaped. A tool that outlives its run is a leak, whatever its "
                "exit code said.",
                owner or "run", len(survivors), list(survivors)[:12],
            )
        return survivors
    except Exception:  # noqa: BLE001 — cleanup must never fail the run it follows
        logger.debug("[ProcessSession] reap degraded", exc_info=True)
        return ()


async def leader_exited(proc: "asyncio.subprocess.Process") -> None:
    """Return when *proc* — the LEADER — has exited. Not when its pipes close.

    ``await proc.wait()`` looks like this and is not. In CPython 3.11 asyncio
    wakes ``wait()`` callers from ``_call_connection_lost``, which runs only
    once EVERY pipe is disconnected — and a pipe is disconnected only when
    every holder of its write end is gone. A descendant that inherited stdout
    therefore blocks ``wait()`` on a leader that died long ago (measured:
    ``returncode == 0`` already visible on the transport, ``wait()`` pending
    six seconds later, and indefinitely). The thing a caller needs in order to
    clean up the descendants is thus withheld BY the descendants.

    A pidfd is the kernel's own statement of the fact: it becomes readable the
    instant the process exits, whatever else is still open. Event-driven via
    ``loop.add_reader`` — no polling. Where pidfds are unavailable (non-Linux,
    kernel < 5.3) this degrades to ``proc.wait()``, i.e. prior behaviour.
    NEVER raises.
    """
    try:
        if proc.returncode is not None:
            return
        opener = getattr(os, "pidfd_open", None)
        if opener is None:
            await proc.wait()
            return
        try:
            fd = opener(proc.pid)
        except ProcessLookupError:
            return  # already exited and reaped
        except OSError:
            await proc.wait()
            return
        loop = asyncio.get_running_loop()
        gone: "asyncio.Future[None]" = loop.create_future()

        def _on_exit() -> None:
            if not gone.done():
                gone.set_result(None)

        try:
            loop.add_reader(fd, _on_exit)
            try:
                await gone
            finally:
                loop.remove_reader(fd)
        finally:
            os.close(fd)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.debug("[ProcessSession] leader-exit wait degraded", exc_info=True)


__all__ = ["leader_exited", "reap_session", "session_survivors"]
