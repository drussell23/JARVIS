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
import threading
from pathlib import Path
from typing import Dict, Optional, Sequence, Set, Tuple

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


def dump_stacks(leader_pid: int) -> bool:
    """Ask a run we are about to kill where it is stuck. NEVER raises.

    ``SIGABRT`` to the leader: pytest enables ``faulthandler`` at configure
    time, so the process writes every thread's stack to its (captured) stderr
    and then dies. That is the only stack a hang killed at the WALL cap can
    leave -- per-test ``pytest-timeout`` never arms during collection, and
    ``reap_session``'s SIGKILL prints nothing. A process without faulthandler
    simply dies, which is what the caller was about to do anyway.

    Sent only while the leader provably holds the id (alive, not our own
    group) -- the same guard ``reap_session`` uses. Returns whether it was sent.
    """
    try:
        pid = int(leader_pid)
        if pid <= 1 or pid == os.getpgrp() or not _leader_alive(pid):
            return False
        os.kill(pid, signal.SIGABRT)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Live sessions — so pressure can end them, and the ending is not misread
# ---------------------------------------------------------------------------

_live_lock = threading.Lock()
_live: Dict[int, str] = {}
_shed: Set[int] = set()


def register_session(leader_pid: int, owner: str = "") -> None:
    """Note a session this process created, for as long as it runs."""
    try:
        with _live_lock:
            _live[int(leader_pid)] = str(owner or "")
    except Exception:  # noqa: BLE001
        pass


def unregister_session(leader_pid: int) -> None:
    try:
        with _live_lock:
            _live.pop(int(leader_pid), None)
    except Exception:  # noqa: BLE001
        pass


def shed_live_sessions(reason: str = "") -> Tuple[int, ...]:
    """End every live session NOW and remember that WE ended it.

    The remembering is the point. A run that dies under ``SIGKILL`` leaves no
    report and an exit code of -9, which downstream reads as an ordinary test
    failure: the model is told its code was wrong and the lesson is recorded.
    ``was_shed`` lets the runner file it where it belongs — infrastructure.
    """
    with _live_lock:
        victims = dict(_live)
    ended = []
    for pid, owner in victims.items():
        with _live_lock:
            _shed.add(pid)
        try:
            os.killpg(pid, signal.SIGKILL)
            ended.append(pid)
        except (ProcessLookupError, PermissionError):
            pass
        except Exception:  # noqa: BLE001
            logger.debug("[ProcessSession] shed degraded pid=%s", pid, exc_info=True)
    if ended:
        logger.warning(
            "[ProcessSession] SHED %d live session(s) (%s): %s",
            len(ended), reason or "memory pressure", ended[:12],
        )
    return tuple(ended)


def was_shed(leader_pid: int) -> bool:
    """Whether *leader_pid* was ended by :func:`shed_live_sessions`. Consumes
    the mark — it is asked once, by whoever is classifying that run."""
    try:
        with _live_lock:
            if int(leader_pid) in _shed:
                _shed.discard(int(leader_pid))
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


# ---------------------------------------------------------------------------
# Session memory budgets — a test that boots the application dies alone
# ---------------------------------------------------------------------------
#
# bt-2026-09-21-225249: the daemon's tree sat at 2.8 GB for 35 minutes, then
# three candidate tests of `jarvis_reload_manager.py` — a module whose job is
# to launch JARVIS — each started a REAL backend inside its sandbox, and 45 s
# later the tree was at 36.9 GB. The daemon's own watchdog caps the WHOLE tree
# and did the only thing it can: stop the daemon. Gracefully, before the host
# felt it — but the soak was over, on the strength of one bad test.
#
# The unit that should die is the SESSION. Each pytest run already owns one
# (start_new_session=True), so its members are enumerable and its memory is
# a sum over them. The budget is not a number written here: it is the daemon
# cap the harness already derives, shared out across the sessions alive at
# that moment. One session gets the whole headroom; three share it. A session
# that exceeds its share is ended and the fact is remembered, so TestRunner
# files the run as what it was — the candidate's failure, with the reason in
# its output — rather than as a timeout nobody can learn from.

_ENV_BUDGET_FLOOR_MB = "JARVIS_SESSION_BUDGET_FLOOR_MB"
_ENV_BUDGET_POLL_FLOOR_S = "JARVIS_SESSION_BUDGET_POLL_FLOOR_S"

_budget_lock = threading.Lock()
_tree_cap_mb: float = 0.0
_tree_poll_base_s: float = 0.0
_over_budget: Dict[int, Tuple[float, float]] = {}
_budget_thread: Optional[threading.Thread] = None
_budget_stop = threading.Event()


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
        return value if value >= minimum else default
    except (TypeError, ValueError):
        return default


def _rss_kb(pid: int) -> int:
    try:
        for line in (Path("/proc") / str(pid) / "status").read_text(errors="replace").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _tree_and_sessions_rss_kb(leaders: Sequence[int]) -> Tuple[int, Dict[int, int]]:
    """One /proc walk: total RSS of this process's tree, and per-session RSS.
    'Tree' here is every process whose session id belongs to one of ours or
    to this process itself — the same population the daemon cap measures."""
    mine = os.getsid(0)
    wanted = {int(pid): 0 for pid in leaders}
    total = 0
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            ids = _stat_ids(entry)
            if ids is None:
                continue
            _pgrp, session, state = ids
            if state == "Z":
                continue
            if session in wanted:
                rss = _rss_kb(pid)
                wanted[session] += rss
                total += rss
            elif session == mine:
                total += _rss_kb(pid)
    except OSError:
        pass
    return total, wanted


def session_budget_mb(tree_rss_mb: float, live_sessions: int) -> float:
    """What ONE session may use right now: the headroom under the daemon
    cap, shared equally among the sessions alive. Adaptive by construction —
    it moves with the daemon's own footprint and with concurrency. A floor
    (env) keeps a crowded moment from starving every session to nothing."""
    with _budget_lock:
        cap = _tree_cap_mb
    if cap <= 0:
        return float("inf")
    headroom = max(0.0, cap - tree_rss_mb)
    share = headroom / max(1, int(live_sessions))
    return max(share, _env_float(_ENV_BUDGET_FLOOR_MB, 512.0, 1.0))


def _budget_tick() -> None:
    with _live_lock:
        leaders = list(_live)
    if not leaders:
        return
    total_kb, per_session = _tree_and_sessions_rss_kb(leaders)
    sessions_kb = sum(per_session.values())
    # Headroom is judged against the tree WITHOUT the sessions, so a session's
    # own growth does not shrink the budget it is measured against.
    base_mb = (total_kb - sessions_kb) / 1024.0
    budget = session_budget_mb(base_mb, len(leaders))
    for leader, kb in per_session.items():
        used = kb / 1024.0
        if used <= budget:
            continue
        with _live_lock:
            owner = _live.get(leader, "")
            _shed.discard(leader)  # this is NOT a pressure shed
        with _budget_lock:
            _over_budget[leader] = (used, budget)
        logger.warning(
            "[ProcessSession] session %s (%s) used %.0f MB against a %.0f MB "
            "budget (daemon cap %.0f MB, %d live session(s)) — ended. The "
            "candidate under test spawned more than its share of the machine.",
            leader, owner or "?", used, budget, _tree_cap_mb, len(leaders),
        )
        reap_session(leader, owner=owner or "session-budget")


def _budget_interval_s(tree_rss_mb: float) -> float:
    """Poll faster as the tree approaches the cap. The daemon watchdog's own
    15 s interval slept through a 34 GB jump; the floor is seconds."""
    with _budget_lock:
        cap, base = _tree_cap_mb, _tree_poll_base_s
    floor = _env_float(_ENV_BUDGET_POLL_FLOOR_S, 1.0, 0.1)
    if cap <= 0 or base <= floor:
        return max(floor, base)
    headroom = max(0.0, min(1.0, (cap - tree_rss_mb) / cap))
    return floor + (base - floor) * headroom


def _budget_loop() -> None:
    while not _budget_stop.is_set():
        try:
            _budget_tick()
            with _live_lock:
                leaders = list(_live)
            total_kb, _ = _tree_and_sessions_rss_kb(leaders) if leaders else (0, {})
            wait = _budget_interval_s(total_kb / 1024.0)
        except Exception:  # noqa: BLE001 — a gauge never dies
            logger.debug("[ProcessSession] budget tick degraded", exc_info=True)
            wait = 5.0
        _budget_stop.wait(wait)


def configure_tree_budget(cap_mb: float, poll_base_s: float) -> bool:
    """Arm session budgets against the daemon's tree cap. Called by whoever
    arms the daemon watchdog, with the SAME cap, so there is one number.
    Idempotent. ``cap_mb <= 0`` disarms."""
    global _budget_thread, _tree_cap_mb, _tree_poll_base_s
    with _budget_lock:
        _tree_cap_mb = float(cap_mb or 0.0)
        _tree_poll_base_s = float(poll_base_s or 0.0)
        armed = _tree_cap_mb > 0
    if not armed or not Path("/proc").is_dir():
        return False
    if _budget_thread is None or not _budget_thread.is_alive():
        _budget_stop.clear()
        _budget_thread = threading.Thread(
            target=_budget_loop, name="session-budget", daemon=True,
        )
        _budget_thread.start()
    logger.warning(
        "[ProcessSession] session memory budgets ARMED — each test session may "
        "use its share of the %.0f MB headroom; one that exceeds it is ended alone",
        _tree_cap_mb,
    )
    return True


def over_budget(leader_pid: int) -> Optional[Tuple[float, float]]:
    """``(used_mb, budget_mb)`` if *leader_pid* was ended for exceeding its
    budget, else ``None``. Consumes the mark."""
    with _budget_lock:
        return _over_budget.pop(int(leader_pid), None)


def reset_budget_for_tests() -> None:
    global _budget_thread
    _budget_stop.set()
    with _budget_lock:
        _over_budget.clear()
    _budget_thread = None


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


__all__ = [
    "leader_exited", "reap_session", "register_session", "session_survivors",
    "shed_live_sessions", "unregister_session", "was_shed",
    "configure_tree_budget", "over_budget", "session_budget_mb",
]
