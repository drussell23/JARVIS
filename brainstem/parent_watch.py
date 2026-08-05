"""The child watches the parent, because the child is the one still alive.

THE PROBLEM
-----------
Xcode's Stop button sends SIGKILL to the HUD. SIGKILL is uncatchable, so
`terminationHandler`, `deinit`, and every `atexit` hook in the parent are
never reached — the parent gets no instructions at all, because it is not
running any more. The Python backend it spawned is reparented to launchd and
lives on, holding ports 8011 and 8742, a microphone claim, and a few hundred
megabytes, until something notices.

Nothing did. Measured 2026-08-04: two orphans, one at 46.8% CPU with no
listening socket, from a debugger detach nobody had thought about.

The compensation that existed was `lsof -ti :8011 | xargs kill -9` at the
NEXT launch. That runs after the damage, and it identifies its victims by
port rather than by identity — anything else bound to 8011 was killed too.

WHY THE FIX BELONGS HERE AND NOT IN THE PARENT
-----------------------------------------------
This is the Watchdog Isolation Invariant one layer down: *a watchdog that
shares a resource with the system it guards is not a watchdog.* A cleanup
handler inside the dying parent shares the parent's fate — SIGKILL means
there is no moment between "alive" and "gone" in which it could run. The only
party guaranteed to still be executing when the parent dies is the child.

So the child watches, and it watches with two independent mechanisms because
a single one is a single point of failure:

  1. `kqueue` / `EVFILT_PROC` / `NOTE_EXIT` — the kernel reports the exit.
     Event-driven, so this costs no CPU while idle, and it fires for SIGKILL
     exactly as it does for a clean exit; the kernel does not care how the
     process died.

  2. `os.getppid()` — when the parent dies the child is reparented, and the
     PPID changes (to 1 under launchd). This shares no code, no file
     descriptor and no syscall with the first mechanism, so a kqueue that
     fails to register, silently drops the event, or is unavailable on some
     future platform does not take the guarantee with it.

Either one firing is sufficient. Both are cheap.

OPT-IN BY POSITIVE DECLARATION
-------------------------------
Supervision is enabled only when a parent PID is explicitly declared through
`JARVIS_PARENT_PID`. Running `python3 -m brainstem` in a terminal to debug
something must never end with the process killing itself because its shell
exited, so silence means "no supervision". This is the same shape as
`JARVIS_PROCESS_ROLE`: a behaviour that can end a process requires somebody
to have asked for it out loud.

SHUTDOWN IS THE EXISTING ONE
-----------------------------
Detection raises SIGTERM into this process rather than exiting. uvicorn
already installs a SIGTERM handler that drains connections and runs lifespan
shutdown, and the legacy `brainstem.main` path installs its own. Inventing a
third shutdown here would mean a second place for shutdown to be wrong.

Escalation exists because graceful shutdown can itself hang — a wedged VERIFY
or a blocked event loop is exactly the condition that produced the orphan in
the first place. After a bounded grace period the process leaves by
`os._exit`, which no amount of blocked event loop can prevent.

The escalation timer reads only `time.monotonic()`. It never consults the
event loop, the application state, or anything the shutdown it is bounding
could wedge — for the same reason the wall-clock watchdog in the battle
harness is forbidden from reading the op-ledger.

WHY THE HARD EXIT FIRES EVERY TIME (MEASURED, NOT YET FIXED)
--------------------------------------------------------------
Escalation is meant to be the exception. It is currently the rule: detection
is instantaneous, and the entire delay is graceful shutdown never finishing.
The dumps below were taken by this module and are what the finding rests on.

    standalone, SIGTERM by hand                          1s, clean
    drained pipe, parent SIGKILLed, no brainstem/.env    0s, clean
    drained pipe, parent SIGKILLed, WITH brainstem/.env  22s, hard exit
    the real HUD                                         11s, hard exit

`.env` is the discriminator, and the reason is what it turns on. At the hard
exit there are twenty-plus long-lived daemon tasks still pending — CloudSQL
cleanup/health/leak monitors, GCP VM monitoring and orphan cleanup, DW
discovery and heavy-probe loops, learning-DB auto-flush and auto-optimise,
the distributed lock cleanup, the background agent pool workers, the rate
orchestrator's forecast and adjustment loops. Without `.env` most of them
never start, and shutdown is instant.

None of them is individually buggy: the ones inspected handle
`asyncio.CancelledError` correctly. **The defect is that nobody owns them.**
They are created ad hoc with `asyncio.create_task` across the codebase, no
shutdown path knows they exist, and they are left for
`asyncio.Runner.close()` to cancel en masse after uvicorn has already
finished. Twenty tasks — several parked on network I/O to CloudSQL, GCP and
DoubleWord — do not all unwind inside any sane grace period. The logs show
`FailoverLifecycle` STARTING new work while shutdown is under way, which is
the same absence of ownership seen from the other end.

The architecture for this already exists and is not wired up:
`backend/core/coordinated_shutdown.py` provides `CoordinatedShutdownManager`
with ordered phases, per-hook timeouts, critical/non-critical hooks and
process-group termination. `backend/main.py`'s lifespan does not call it —
it hand-rolls 535 sequential lines instead, with roughly twenty awaits that
have no timeout at all. Fixing this means registering those tasks as hooks,
not writing a new shutdown system.

Until that lands, this module's hard exit is what keeps a dead launcher from
leaving a live backend, and every occurrence is recorded with the stacks and
the task list that explain it.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from typing import Optional

logger = logging.getLogger("jarvis.brainstem.parentwatch")

PARENT_WATCH_SCHEMA_VERSION: str = "parent_watch.v1"

#: Set by the launcher to its own PID. Absent means "not supervised".
ENV_PARENT_PID: str = "JARVIS_PARENT_PID"

#: Master switch. Default on, but inert without a declared parent PID.
ENV_ENABLED: str = "JARVIS_PARENT_WATCH_ENABLED"

#: How long graceful shutdown gets before the process leaves the hard way.
ENV_GRACE_S: str = "JARVIS_PARENT_WATCH_GRACE_S"

#: Backstop cadence. Only the PPID poll uses this; kqueue is event-driven.
ENV_POLL_S: str = "JARVIS_PARENT_WATCH_POLL_S"

#: Exit status when escalation fires. 143 = 128 + SIGTERM, the conventional
#: "terminated by SIGTERM" code, so a supervisor reading exit codes sees the
#: shutdown that was intended rather than a novel number.
EXIT_PARENT_GONE: int = 143


#: Where this subsystem records what it did, independently of everything else.
ENV_LOG_PATH: str = "JARVIS_PARENT_WATCH_LOG"

#: Set once stdout/stderr have been pointed at the log file. After that, fd 2
#: IS the log file, so writing to both would record every line twice.
_DETACHED: bool = False


def _default_log_path() -> str:
    return os.path.join(os.path.expanduser("~/Library/Logs/JARVIS"),
                        "parent-watch.log")


def _record(message: str) -> None:
    """Write a line using only syscalls. NEVER raises, NEVER blocks on a lock.

    TWO REASONS THIS DOES NOT USE `logging`
    ----------------------------------------
    1. NOWHERE TO SEND IT. This process logs to a pipe held by its parent, and
       every message this subsystem has to deliver is about that parent being
       dead. Measured 2026-08-04: the watch fired correctly, the backend exited
       correctly, and the log recorded NOTHING — because the reader had already
       been SIGKILLed. A subsystem whose only job is to explain a disappearance
       must not report through the thing that disappeared.

    2. `logging` TAKES LOCKS. The escalation path exists to bound a shutdown
       that has WEDGED, and a wedged shutdown is exactly the state in which
       some thread is holding the logging lock. Calling `logger.error` there
       would block the watchdog on the system it is watching — the Watchdog
       Isolation Invariant, which says a watchdog sharing a signal path, a
       logging lock, or a state-ledger with its subject is not a watchdog.

    So: `os.open`/`os.write`, append mode, no buffering, no locks, no
    formatters. If it fails, it fails silently — a log line is never worth
    obstructing the exit it is describing.
    """
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [ParentWatch] {message}\n"
    data = line.encode("utf-8", "replace")
    if not _DETACHED:
        # Skipped once detached: fd 2 has been dup2'd onto the log file, so
        # this write and the one below would be the same line, twice.
        try:
            os.write(2, data)      # stderr, for when anyone is still reading
        except Exception:  # noqa: BLE001
            pass
    try:
        path = (os.environ.get(ENV_LOG_PATH, "") or "").strip() or _default_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001
        pass


def _dump_stacks() -> None:
    """Record every thread's stack at the moment of the hard exit. NEVER raises.

    A hard exit that says only "shutdown did not finish" reports a symptom and
    destroys the evidence in the same instant. This is the one moment the
    answer is still in memory, and it is the last moment anybody can ask.

    `faulthandler.dump_traceback` is used rather than `traceback` because it
    writes to a raw file descriptor from C, taking no Python-level lock and
    allocating nothing. It is designed to work in exactly the state that makes
    this necessary — a process too wedged to run ordinary Python reliably.
    Using `traceback.format_stack` here would mean acquiring the very locks
    the wedge may be holding, which is how a diagnostic becomes the hang.
    """
    try:
        import faulthandler
        path = (os.environ.get(ENV_LOG_PATH, "") or "").strip() or _default_log_path()
        _record("--- thread stacks at hard exit (what shutdown was waiting on) ---")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            faulthandler.dump_traceback(file=fd, all_threads=True)
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001
        pass


def _dump_pending_tasks(limit: int = 40) -> None:
    """Name the asyncio tasks that were still alive at the hard exit.

    Thread stacks are not enough. The measured wedge was the MAIN thread
    sitting in `asyncio.runners._cancel_all_tasks`, which cancels every
    remaining task and then waits for all of them to finish — so the thread
    stack says "waiting for tasks" and stops exactly where the interesting
    part begins. A coroutine that swallows `CancelledError`, or that is
    blocked somewhere cancellation cannot reach, holds the process open
    forever and appears in no thread dump.

    Found by walking the garbage collector rather than `asyncio.all_tasks()`,
    which must be called from the loop's own thread — the thread that is, by
    construction, the one that is stuck. This costs a full heap scan, which
    would be indefensible anywhere except here: the process is already leaving,
    and this is the last instant the answer exists.
    """
    try:
        import asyncio
        import gc
        alive = []
        for obj in gc.get_objects():
            try:
                if isinstance(obj, asyncio.Task) and not obj.done():
                    alive.append(obj)
            except Exception:  # noqa: BLE001
                continue
        if not alive:
            _record("no pending asyncio tasks — the wedge is not task cancellation")
            return
        _record(f"--- {len(alive)} pending asyncio task(s) at hard exit "
                f"(these are what _cancel_all_tasks was waiting for) ---")
        for t in alive[:limit]:
            try:
                coro = t.get_coro()
                name = getattr(coro, "__qualname__", None) or repr(coro)
                where = ""
                frame = getattr(coro, "cr_frame", None)
                if frame is not None:
                    where = (f" at {os.path.basename(frame.f_code.co_filename)}"
                             f":{frame.f_lineno}")
                _record(f"    cancelling={t.cancelled()} name={t.get_name()} "
                        f"coro={name}{where}")
            except Exception:  # noqa: BLE001
                continue
        if len(alive) > limit:
            _record(f"    ... and {len(alive) - limit} more")
    except Exception:  # noqa: BLE001
        pass


def _detach_dead_output() -> bool:
    """Point stdout/stderr somewhere that cannot block. NEVER raises.

    WHAT THIS IS, AND WHAT IT IS NOT
    ----------------------------------
    It is NOT the fix for the slow shutdown. That was measured and disproved:
    a faithful repro — a parent that DRAINS the pipe exactly as
    `readabilityHandler` does, then is SIGKILLed — shuts the backend down in
    **0 seconds** with or without this. The real cause is elsewhere and is
    recorded in the module docstring.

    What it does fix is a real and separately measured failure: a pipe that is
    NOT being drained. A 64KB buffer fills, the child blocks in `write()`, and
    everything downstream of that write stops:

        undrained pipe, reader killed   17s, never completed, 17 tasks pending
        file sink, reader killed         1s, clean

    That is not hypothetical. A HUD that is alive but has stopped reading — a
    beachball, a suspended debugger, a paused process under Instruments — puts
    the backend in exactly that state, and no watch fires because nobody has
    died. Severing the connection when the reader is known dead removes one
    version of it cheaply.

    The general form of the defect is worth stating plainly: **a process's
    ability to make progress must not depend on a consumer of its logs.** The
    complete answer is for the launcher to hand the child a FILE rather than a
    pipe, so no reader can ever apply backpressure. That is a Swift-side
    change, and it is the honest fix for the class.

    By the time this runs the watch has already established that the reader is
    dead — that is the entire reason it fired — so this severs a connection to
    a corpse and loses no output that was still going anywhere. The
    replacement is the watch's own log file rather than `/dev/null`, because
    what arrives now is shutdown's account of itself.
    """
    try:
        path = (os.environ.get(ENV_LOG_PATH, "") or "").strip() or _default_log_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        except Exception:  # noqa: BLE001
            # Anywhere that accepts writes beats a pipe nobody is reading.
            fd = os.open(os.devnull, os.O_WRONLY)
        try:
            # dup2 onto the raw descriptors, so every writer is covered at once
            # — `sys.stdout`, `sys.stderr`, C extensions, and any handler that
            # captured `sys.__stderr__` at import time and would otherwise keep
            # its own reference to the dead pipe.
            os.dup2(fd, 1)
            os.dup2(fd, 2)
        finally:
            if fd > 2:
                os.close(fd)
        global _DETACHED
        _DETACHED = True
        return True
    except Exception:  # noqa: BLE001
        return False


def _enabled() -> bool:
    return (os.environ.get(ENV_ENABLED, "true") or "").strip().lower() not in (
        "0", "false", "no", "off")


def _float_env(name: str, default: float, *, minimum: float) -> float:
    """Read a tunable, clamped. NEVER raises.

    Clamped rather than validated-and-rejected: a typo in a timing knob must
    not be able to disable the guarantee. A grace of 0 would make graceful
    shutdown impossible and a poll of 0 would spin a core.
    """
    try:
        raw = (os.environ.get(name, "") or "").strip()
        if not raw:
            return default
        return max(minimum, float(raw))
    except Exception:  # noqa: BLE001
        return default


def declared_parent_pid() -> Optional[int]:
    """The PID the launcher declared, or None. NEVER raises.

    Rejects values that cannot be a parent we should die with:

      * unparseable or non-positive — a malformed declaration is not a
        declaration;
      * our own PID — watching ourselves would fire the moment we exit;
      * PID 1 — launchd is everybody's eventual parent, so treating it as
        "the parent" would mean self-terminating exactly when reparenting
        has ALREADY happened, which is the state this guards against, not a
        thing to wait for.
    """
    try:
        raw = (os.environ.get(ENV_PARENT_PID, "") or "").strip()
        if not raw:
            return None
        pid = int(raw)
        if pid <= 1 or pid == os.getpid():
            logger.warning(
                "[ParentWatch] ignoring implausible parent pid %d — "
                "supervision NOT active", pid)
            return None
        return pid
    except Exception:  # noqa: BLE001
        logger.warning("[ParentWatch] unparseable %s — supervision NOT active",
                       ENV_PARENT_PID)
        return None


def _parent_is_alive(pid: int) -> bool:
    """Signal 0 probes existence without delivering anything. NEVER raises.

    A `PermissionError` means the process exists and belongs to somebody else
    — existence is what is being asked, so that is alive. Treating it as dead
    would kill the backend whenever the launcher runs as another user.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001
        return True


class ParentWatch:
    """Terminates this process when the declared parent dies. NEVER raises."""

    def __init__(self, parent_pid: int) -> None:
        self.parent_pid = parent_pid
        self.original_ppid = os.getppid()
        self.grace_s = _float_env(ENV_GRACE_S, 10.0, minimum=1.0)
        self.poll_s = _float_env(ENV_POLL_S, 2.0, minimum=0.25)
        self._fired = threading.Event()
        self._fired_at = 0.0
        self._threads: list = []

    # -- detection -------------------------------------------------------

    def _watch_kqueue(self) -> None:
        """Block until the kernel reports the parent exited. NEVER raises.

        Returns early — leaving the PPID poll as the sole mechanism — when
        kqueue is unavailable or registration fails for any reason other than
        the parent already being gone.
        """
        try:
            import select
            if not hasattr(select, "kqueue"):
                logger.debug("[ParentWatch] no kqueue; PPID poll is sole watch")
                return
            kq = select.kqueue()
            ev = select.kevent(
                self.parent_pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                fflags=select.KQ_NOTE_EXIT,
            )
            try:
                kq.control([ev], 0, 0)
            except ProcessLookupError:
                # Already dead — the race between spawn and registration is
                # not an error, it is the answer.
                self._fire("kqueue: parent already gone at registration")
                return
            _record(f"armed on pid {self.parent_pid} "
                    f"(kqueue NOTE_EXIT + PPID poll every {self.poll_s:.2f}s, "
                    f"grace {self.grace_s:.1f}s) — this process will not "
                    f"outlive its launcher")
            while not self._fired.is_set():
                # Bounded wait so a fired watch can retire this thread even
                # though the kernel has nothing to say.
                events = kq.control(None, 1, self.poll_s)
                for _ in events:
                    self._fire("kqueue: NOTE_EXIT")
                    return
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ParentWatch] kqueue watch degraded: %s", exc)

    def _watch_ppid(self) -> None:
        """Independent backstop: reparenting and an existence probe.

        Two conditions, because each covers a hole in the other. A changed
        PPID is definitive but only observable while we are a direct child;
        the existence probe still answers if the declared parent was never
        our immediate parent (a relaunch through a helper, say).
        """
        while not self._fired.is_set():
            if self._fired.wait(self.poll_s):
                return
            try:
                if os.getppid() != self.original_ppid:
                    self._fire(
                        f"reparented: ppid {self.original_ppid} -> {os.getppid()}")
                    return
                if not _parent_is_alive(self.parent_pid):
                    self._fire(f"pid {self.parent_pid} no longer exists")
                    return
            except Exception as exc:  # noqa: BLE001
                logger.debug("[ParentWatch] ppid poll degraded: %s", exc)

    # -- reaction --------------------------------------------------------

    def _fire(self, reason: str) -> None:
        """Begin shutdown. Idempotent — whichever mechanism wins, once.

        `Event.set` is atomic, so the loser of the race returns here and does
        nothing. Without that, two detections would raise two SIGTERMs and
        start two escalation timers.
        """
        if self._fired.is_set():
            return
        self._fired.set()
        self._fired_at = time.monotonic()
        # BEFORE the SIGTERM, not after. Shutdown begins the instant the signal
        # is delivered, and it is shutdown's own logging that blocks on the
        # dead pipe — redirecting afterwards would race the thing it prevents.
        # The token, before anything else. This thread learns the launcher is
        # gone microseconds before SIGTERM is delivered, and that head start is
        # exactly the window in which a subsystem would otherwise spawn one
        # more recovery task for shutdown to wait on.
        try:
            from backend.core.app_lifecycle import LIFECYCLE
            LIFECYCLE.request_shutdown(f"launcher gone: {reason}")
        except Exception:  # noqa: BLE001 — the watch never depends on the app
            pass
        detached = _detach_dead_output()
        _record(f"launcher (pid {self.parent_pid}) is gone — {reason}. "
                f"Raising SIGTERM; hard exit in {self.grace_s:.1f}s if that "
                f"does not finish. Better an abrupt exit than an orphan "
                f"holding ports, a microphone claim and memory. "
                f"stdout/stderr detached from the dead reader: {detached}")
        threading.Thread(target=self._escalate, name="parent-watch-escalate",
                         daemon=True).start()
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception as exc:  # noqa: BLE001
            _record(f"could not raise SIGTERM ({exc}) — escalation will handle it")

    def _escalate(self) -> None:
        """Leave the hard way if graceful shutdown does not finish.

        Reads a clock and nothing else. The condition this bounds — a wedged
        event loop — is precisely the condition that would make any
        application-state check hang with it.
        """
        deadline = time.monotonic() + self.grace_s
        while time.monotonic() < deadline:
            time.sleep(0.25)
        # Reaching here means graceful shutdown did NOT finish. That is worth
        # recording precisely: if this line appears every time, the exit is
        # being done by the axe rather than by uvicorn, and something in
        # shutdown is wedged and wants fixing on its own terms.
        _record(f"graceful shutdown did not complete within {self.grace_s:.1f}s "
                f"— exiting hard (status {EXIT_PARENT_GONE}). An orphan that "
                f"will not die is worse than an abrupt exit.")
        _dump_stacks()
        _dump_pending_tasks()
        os._exit(EXIT_PARENT_GONE)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> "ParentWatch":
        """Arm both mechanisms on daemon threads. NEVER raises."""
        # Checked before arming: if the launcher died between spawning us and
        # our reaching this line, no future event will ever be delivered
        # because the exit has already happened.
        if not _parent_is_alive(self.parent_pid):
            self._fire("parent was already gone before the watch armed")
            return self
        for target, name in ((self._watch_kqueue, "parent-watch-kqueue"),
                             (self._watch_ppid, "parent-watch-ppid")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        """Retire the watch. For tests and for a deliberate detach."""
        self._fired.set()

    @property
    def fired(self) -> bool:
        return self._fired.is_set()


def install() -> Optional[ParentWatch]:
    """Arm the watch if a parent was declared. NEVER raises.

    Returns None when supervision is not active, which is the normal state
    for a standalone run and is not a failure.
    """
    try:
        if not _enabled():
            logger.info("[ParentWatch] disabled by %s", ENV_ENABLED)
            return None
        pid = declared_parent_pid()
        if pid is None:
            logger.info(
                "[ParentWatch] no %s declared — running unsupervised. This is "
                "correct for a standalone launch; the HUD declares it.",
                ENV_PARENT_PID)
            return None
        return ParentWatch(pid).start()
    except Exception:  # noqa: BLE001
        logger.error("[ParentWatch] install failed — this process may outlive "
                     "its launcher", exc_info=True)
        return None
