"""An external binary may cost us time. It may never cost us a thread.

THE BLIND SPOT OFFLOADING CREATED
---------------------------------
Moving `osascript` and `screencapture` off the event loop fixed the stalls and
opened a worse failure mode: the blocking call now happens on a POOL worker.
The loop keeps breathing, so nothing looks wrong — while a wedged binary holds
a worker forever. Repeat it on a cadence and the pool bleeds out silently.
That is strictly worse than the stall it replaced, because a stall is visible
and a leaked worker is not.

WHY `subprocess.run(timeout=...)` IS NOT ENOUGH
-----------------------------------------------
It kills the direct CHILD and then calls `communicate()` again to reap it.
Two things defeat that here:

* The child spawns grandchildren. `_get_cursor_position` runs an `osascript`
  whose script does `do shell script "python3 -c ..."`. SIGKILL to the
  osascript leaves the python interpreter alive, holding the inherited pipe
  open — so the post-kill `communicate()` blocks on a pipe that never closes,
  and the worker is captured anyway.
* On macOS a binary blocked in the kernel on a TCC (screen-recording /
  automation) consent prompt is not reliably reaped by a plain kill: the
  prompt belongs to the window server, not to us.

So the child is started in its OWN process group (`start_new_session=True`)
and the whole GROUP is signalled — TERM first, then KILL — and the reap
itself is bounded. If even that does not return, the pipes are abandoned
rather than waited on: losing a file descriptor is survivable, losing the
worker permanently is not.

THE CIRCUIT BREAKER
-------------------
A binary that hangs once will hang again — a revoked TCC permission does not
heal because we retried. After `JARVIS_SUBPROC_BREAKER_TRIPS` consecutive
timeouts for one command, that command is refused outright for
`JARVIS_SUBPROC_BREAKER_COOLDOWN_S`, returning None immediately without
touching a worker at all. A single success closes the breaker.

Keyed on the BINARY, not on the full argv: `screencapture` hanging on a
consent prompt hangs for every temp path, so keying on the path would open a
fresh breaker per call and never trip. See :func:`_key` for why it is not
keyed on a guessed sub-verb either — a flag and a sub-verb are not
distinguishable by shape, and the first attempt's own test proved it.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger("Jarvis.BoundedSubprocess")

BOUNDED_SUBPROCESS_SCHEMA_VERSION: str = "bounded_subprocess.1"

__all__ = [
    "BOUNDED_SUBPROCESS_SCHEMA_VERSION",
    "breaker_ledger_path",
    "breaker_open",
    "breaker_state",
    "open_breakers_on_disk",
    "reset_for_tests",
    "run_bounded",
]


def _num(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float((os.environ.get(name) or "").strip()
                                     or default)))
    except Exception:  # noqa: BLE001
        return default


def _breaker_trips() -> int:
    """Consecutive timeouts before a command is refused."""
    return int(_num("JARVIS_SUBPROC_BREAKER_TRIPS", 3, 1, 100))


def _breaker_cooldown_s() -> float:
    """How long a tripped command stays refused. Default 5 minutes."""
    return _num("JARVIS_SUBPROC_BREAKER_COOLDOWN_S", 300.0, 1.0, 86400.0)


def _reap_grace_s() -> float:
    """Time between TERM and KILL for the process group."""
    return _num("JARVIS_SUBPROC_REAP_GRACE_S", 0.5, 0.05, 10.0)


def _reap_budget_s() -> float:
    """Total time spent reaping before the pipes are abandoned.

    Bounded because the reap is the last thing standing between a wedged
    binary and a captured worker; an unbounded wait here would recreate the
    exact leak this module exists to prevent.
    """
    return _num("JARVIS_SUBPROC_REAP_BUDGET_S", 2.0, 0.1, 30.0)


# ---------------------------------------------------------------------------
# Breaker state — keyed by command, not by argv
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_fails: Dict[str, int] = {}
_opened_at: Dict[str, float] = {}


def _key(cmd: Sequence[str]) -> str:
    """The BINARY, and nothing else.

    Never the full argv: a hang caused by a consent prompt reproduces for
    every temp path, so keying on the path would open a fresh breaker per
    call and never trip.

    And never a guessed sub-verb either. A first attempt keyed
    `binary + argv[1]` when argv[1] looked short and flag-like, to keep
    `yabai -m` distinct from `yabai --start-service`. Its own test found the
    flaw immediately: that rule reads `screencapture -x -t png <path>` as
    `screencapture -x`, because a FLAG and a SUB-VERB are not distinguishable
    by shape. Generic argv parsing is not knowable here, so the rule that
    remains is the one that is always true — this binary hangs.

    The granularity that was lost costs nothing today: only the probing
    calls are routed through here, never the recovery ones, so a tripped
    breaker cannot refuse the command that would fix the condition. If a
    recovery path is ever routed through this, it needs its own key —
    stated here because that is exactly the kind of coupling that is
    invisible until it bites.
    """
    try:
        parts = [str(c) for c in cmd if str(c)]
        return os.path.basename(parts[0]) if parts else "?"
    except Exception:  # noqa: BLE001
        return "?"



# ---------------------------------------------------------------------------
# Durable export — the breaker must be visible from ANOTHER process
# ---------------------------------------------------------------------------
#
# A tripped breaker that only exists in the daemon's memory is invisible to
# `trinity status`, which runs in its own CLI process. A revoked macOS
# Screen-Recording permission would then present as "everything fine, the
# screenshots merely stopped" — the silent failure this whole arc exists to
# end. So a trip is written where any process can read it, and cleared the
# moment the command succeeds again.


def breaker_ledger_path() -> Path:
    """Where open breakers are recorded. ``JARVIS_SUBPROC_BREAKER_LEDGER``,
    else beside the other `.jarvis/` state."""
    raw = (os.environ.get("JARVIS_SUBPROC_BREAKER_LEDGER") or "").strip()
    if raw:
        return Path(raw)
    root = Path(os.environ.get("JARVIS_PROJECT_ROOT") or ".")
    return root / ".jarvis" / "subprocess_breakers.json"


def _write_ledger() -> None:
    """Persist the open set. NEVER raises — telemetry may not break a probe."""
    try:
        path = breaker_ledger_path()
        with _lock:
            payload = {
                "schema_version": BOUNDED_SUBPROCESS_SCHEMA_VERSION,
                "open": {k: {"opened_at_monotonic": v,
                             "consecutive_failures": _fails.get(k, 0)}
                         for k, v in _opened_at.items()},
                "updated_at": time.time(),
                "pid": os.getpid(),
                "cooldown_s": _breaker_cooldown_s(),
            }
        if not payload["open"]:
            # An empty ledger is REMOVED rather than written empty: a stale
            # file full of `{}` and an actually-healthy system should not be
            # distinguishable only by reading a timestamp.
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        logger.debug("[BoundedSubprocess] ledger write skipped", exc_info=True)


def open_breakers_on_disk(path: Optional[Path] = None) -> Tuple[str, ...]:
    """Commands currently refused, as recorded by ANY process. NEVER raises.

    Read by `trinity status` so a tripped breaker in the daemon shows up on
    the operator's screen instead of only in that daemon's memory. A ledger
    older than the cooldown is ignored: the breaker would have re-probed by
    now, and reporting a stale trip is its own kind of lie.
    """
    try:
        p = path or breaker_ledger_path()
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return ()
        age = time.time() - float(raw.get("updated_at") or 0)
        if age > float(raw.get("cooldown_s") or _breaker_cooldown_s()) * 2:
            return ()
        return tuple(sorted(raw.get("open") or ()))
    except Exception:  # noqa: BLE001 — absent / malformed == nothing open
        return ()



_hydrated = False


def _hydrate_from_ledger() -> None:
    """Adopt trips recorded by a PREVIOUS process, once. NEVER raises.

    Found by testing recovery rather than by reading code: a success in a
    fresh process could not clear an on-disk trip, because `_record_success`
    only rewrote the ledger when the key was open in THIS process's memory —
    and a new process has none.

    Hydrating fixes both directions, and the second is the more valuable: a
    daemon that restarts while a binary is still wedged should honour the
    cooldown it already earned instead of spending a worker rediscovering
    it. The staleness rule in `open_breakers_on_disk` still applies, so an
    ancient ledger is ignored rather than inherited.
    """
    global _hydrated  # noqa: PLW0603
    if _hydrated:
        return
    _hydrated = True
    try:
        for key in open_breakers_on_disk():
            with _lock:
                _opened_at.setdefault(key, time.monotonic())
                _fails.setdefault(key, _breaker_trips())
    except Exception:  # noqa: BLE001
        pass


def breaker_open(cmd: Sequence[str]) -> bool:
    """Is this command currently refused? NEVER raises."""
    try:
        _hydrate_from_ledger()
        key = _key(cmd)
        with _lock:
            opened = _opened_at.get(key)
            if opened is None:
                return False
            # Cooldown elapsed -> allow a probe through, but do NOT clear the
            # state here. This function is a QUESTION, and it used to answer
            # by mutating: it popped `_opened_at`, so the success that
            # followed found nothing open and never cleared the on-disk
            # ledger — the breaker recovered in memory and stayed tripped on
            # the operator's screen forever. Only an OUTCOME
            # (`_record_success` / `_record_timeout`) changes state now.
            return (time.monotonic() - opened) < _breaker_cooldown_s()
    except Exception:  # noqa: BLE001
        return False


def breaker_state() -> Dict[str, Any]:
    """Bounded projection for operator surfaces. NEVER raises."""
    with _lock:
        return {
            "schema_version": BOUNDED_SUBPROCESS_SCHEMA_VERSION,
            "consecutive_failures": dict(_fails),
            "open": sorted(_opened_at),
            "trips_to_open": _breaker_trips(),
            "cooldown_s": _breaker_cooldown_s(),
        }


def reset_for_tests() -> None:
    global _hydrated  # noqa: PLW0603
    _hydrated = True                 # tests own their state; never adopt disk
    with _lock:
        _fails.clear()
        _opened_at.clear()


def _record_success(key: str) -> None:
    _hydrate_from_ledger()
    reopened = False
    with _lock:
        _fails.pop(key, None)
        reopened = _opened_at.pop(key, None) is not None
    if reopened:
        _write_ledger()


def _record_timeout(key: str) -> None:
    newly_open = False
    with _lock:
        _fails[key] = _fails.get(key, 0) + 1
        if _fails[key] >= _breaker_trips() and key not in _opened_at:
            _opened_at[key] = time.monotonic()
            newly_open = True
            failures = _fails[key]
    if newly_open:
        logger.warning(
            "[BoundedSubprocess] breaker OPEN for %r after %d consecutive "
            "timeouts — refusing it for %.0fs rather than spending another "
            "worker on it", key, failures, _breaker_cooldown_s())
        # Written OUTSIDE the lock: the ledger touches the filesystem, and a
        # disk hiccup must not hold the lock every probe contends for.
        _write_ledger()


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


def _reap(proc: "subprocess.Popen") -> None:
    """Kill the process GROUP and stop waiting on it. NEVER raises.

    The group, not the process: a child that spawned grandchildren leaves
    them holding the inherited pipe, and the post-kill wait then blocks on a
    pipe that never closes — capturing the very worker the kill was meant to
    free.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:  # noqa: BLE001 — already gone
        pgid = None

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        deadline = time.monotonic() + (
            _reap_grace_s() if sig == signal.SIGTERM else _reap_budget_s())
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.02)

    # Still not dead. Abandon the pipes rather than block here forever: a
    # leaked file descriptor is survivable, a permanently captured worker is
    # not.
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        try:
            if stream is not None:
                stream.close()
        except Exception:  # noqa: BLE001
            pass
    logger.warning("[BoundedSubprocess] pid %s survived TERM+KILL — pipes "
                   "abandoned to release the worker", proc.pid)


def run_bounded(cmd: Sequence[str], *, timeout: float,
                text: bool = False) -> Optional[subprocess.CompletedProcess]:
    """Run *cmd* with a hard ceiling. NEVER raises, NEVER holds a worker.

    Returns the CompletedProcess, or None when the command timed out, could
    not start, or is currently refused by the breaker. None means "no
    answer" — every caller here already treats an unanswered probe as
    unknown, which is the honest reading and keeps a wedged binary from
    being mistaken for a negative result.
    """
    key = _key(cmd)
    if breaker_open(cmd):
        logger.debug("[BoundedSubprocess] %r refused (breaker open)", key)
        return None

    proc = None
    try:
        proc = subprocess.Popen(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            # Its OWN process group, so the whole tree can be signalled.
            start_new_session=True,
        )
        out, err = proc.communicate(timeout=timeout)
        _record_success(key)
        return subprocess.CompletedProcess(list(cmd), proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        logger.warning("[BoundedSubprocess] %r exceeded %.2fs — reaping its "
                       "process group", key, timeout)
        if proc is not None:
            _reap(proc)
        _record_timeout(key)
        return None
    except (FileNotFoundError, PermissionError, OSError) as exc:
        # A missing binary is a deterministic answer, not a hang: it must not
        # count toward the breaker, or an uninstalled tool would look like a
        # wedged one.
        logger.debug("[BoundedSubprocess] %r unavailable: %s", key, exc)
        if proc is not None:
            _reap(proc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("[BoundedSubprocess] %r failed: %s", key, exc,
                     exc_info=True)
        if proc is not None:
            _reap(proc)
        return None
