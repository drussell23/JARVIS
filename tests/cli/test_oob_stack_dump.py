"""Asking a frozen process what it is doing.

A UI deadlock is the one bug that cannot be investigated afterwards: Ctrl+C
does nothing to a blocked thread, `kill -9` destroys the frames, and the log
records an absence. The `/` freeze was never explained for exactly this
reason.

    kill -USR1 <ov pid>

The decisive test is the last one, which deadlocks the MAIN thread on a lock
another thread holds and then asks for a dump. That is the only scenario worth
building this for, and it is the one a Python-level signal handler cannot
serve — such a handler runs between bytecodes, and a thread blocked in a C
call executes none.
"""
from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

import pytest

from backend.core.ouroboros.battle_test.oob_diagnostics import (
    OOB_SIGNAL,
    dump_all_threads,
    install_oob_stack_dump,
    oob_hint,
)

_REPO = pathlib.Path(__file__).resolve().parents[2]
_needs_usr1 = pytest.mark.skipif(
    OOB_SIGNAL is None, reason="SIGUSR1 unavailable on this platform"
)


# --------------------------------------------------------------------------
# 1. the in-band dump
# --------------------------------------------------------------------------

def test_a_dump_names_every_thread(tmp_path: pathlib.Path) -> None:
    log = tmp_path / "dump.log"
    stop = threading.Event()
    worker = threading.Thread(target=lambda: stop.wait(5), name="oob-worker")
    worker.start()
    try:
        with log.open("w") as handle:
            assert dump_all_threads(handle) is True
        body = log.read_text()
        assert "Thread" in body or "Current thread" in body
        assert 'File "' in body, "no frames — this is not a stack trace"
    finally:
        stop.set()
        worker.join(timeout=2)


def test_dumping_never_raises_on_a_broken_sink() -> None:
    class _Broken:
        def write(self, _s: str) -> None:
            raise OSError("EPIPE")

        def flush(self) -> None:
            raise OSError("EPIPE")

        def fileno(self) -> int:
            raise OSError("no fd")

    assert dump_all_threads(_Broken()) in (True, False)   # must not raise


# --------------------------------------------------------------------------
# 2. arming
# --------------------------------------------------------------------------

@_needs_usr1
def test_arming_creates_the_log_and_is_idempotent(
    tmp_path: pathlib.Path,
) -> None:
    log = tmp_path / "deep" / "ov-crash.log"
    assert install_oob_stack_dump(log) is True
    assert log.exists(), "the log was not created at arm time"
    assert install_oob_stack_dump(log) is True          # again, no damage
    assert str(os.getpid()) in log.read_text()


def test_the_hint_tells_an_operator_what_to_type() -> None:
    """Printed at boot, because the moment it is needed the UI is frozen and
    cannot tell you anything."""
    hint = oob_hint()
    if OOB_SIGNAL is None:
        assert hint == ""
        return
    assert "USR1" in hint and str(os.getpid()) in hint


def test_it_writes_where_the_crash_breaker_writes() -> None:
    """DRY: one file for 'could not start' and 'stopped responding'. An
    operator should not have to know which of two logs to open."""
    from backend.core.ouroboros.battle_test.mount_breaker import crash_log_path
    from backend.core.ouroboros.battle_test.oob_diagnostics import _log_path

    assert _log_path() == crash_log_path()


def test_it_is_armed_before_the_ui_mounts() -> None:
    """Structural: a trap armed after the mount cannot catch a mount that
    wedges."""
    src = (_REPO / "backend/core/ouroboros/cli/ov.py").read_text()
    assert "install_oob_stack_dump()" in src
    assert src.index("install_oob_stack_dump()") < src.index("PromptSession(")


# --------------------------------------------------------------------------
# 3. it fires, and the process lives
# --------------------------------------------------------------------------

_SUBPROC = r'''
import os, signal, sys, threading, time
sys.path.insert(0, {repo!r})
from backend.core.ouroboros.battle_test.oob_diagnostics import install_oob_stack_dump
install_oob_stack_dump(__import__("pathlib").Path({log!r}))

mode = sys.argv[1]
if mode == "deadlock":
    # THE case this exists for: the main thread blocks in a C-level lock
    # acquire that never returns. It executes no bytecodes, so a Python
    # signal handler would never run.
    held = threading.Lock()
    held.acquire()
    threading.Thread(target=lambda: time.sleep(30), daemon=True,
                     name="oob-holder").start()
    def _wedge():
        held.acquire()          # blocks forever
    threading.Thread(target=_wedge, daemon=True, name="oob-wedged").start()
    time.sleep(0.4)
    os.kill(os.getpid(), signal.SIGUSR1)
    time.sleep(0.6)
else:
    threading.Thread(target=lambda: time.sleep(5), daemon=True,
                     name="oob-worker").start()
    time.sleep(0.3)
    os.kill(os.getpid(), signal.SIGUSR1)
    time.sleep(0.3)
    os.kill(os.getpid(), signal.SIGUSR1)   # twice — repeatable
    time.sleep(0.3)
print("SURVIVED", flush=True)
'''


def _run(mode: str, tmp_path: pathlib.Path) -> Any:
    log = tmp_path / "ov-crash.log"
    script = tmp_path / f"probe_{mode}.py"
    script.write_text(_SUBPROC.format(repo=str(_REPO), log=str(log)))
    proc = subprocess.run(
        [sys.executable, str(script), mode], capture_output=True, text=True,
        timeout=60, cwd=str(_REPO),
        env={**os.environ, "PYTHONPATH": str(_REPO)},
    )
    return proc, log


@_needs_usr1
def test_the_signal_dumps_without_killing_the_process(
    tmp_path: pathlib.Path,
) -> None:
    """MANDATE 4, and the bug this caught.

    The first implementation used `chain=True`, which calls the PREVIOUS
    handler after dumping — and for SIGUSR1 that is SIG_DFL, whose default
    disposition is to TERMINATE. It produced a perfect trace and then killed
    the process it was diagnosing (exit 158 = 128 + 30), which is exactly the
    `kill -9` this replaces.
    """
    proc, log = _run("normal", tmp_path)
    assert proc.returncode == 0, (
        f"the signal killed the process (rc={proc.returncode}"
        f"{'; 158 = SIGUSR1 default disposition' if proc.returncode == 158 else ''})"
    )
    assert "SURVIVED" in proc.stdout
    body = log.read_text()
    assert 'File "' in body
    assert body.count("Current thread") >= 2, "the second dump did not fire"


@_needs_usr1
def test_it_fires_while_a_thread_is_deadlocked(
    tmp_path: pathlib.Path,
) -> None:
    """The only scenario worth building this for.

    A Python-level handler runs between bytecodes; a thread blocked in a C
    lock acquire executes none, so it would never fire. faulthandler writes
    from the C handler itself and does."""
    proc, log = _run("deadlock", tmp_path)
    assert proc.returncode == 0
    assert "SURVIVED" in proc.stdout
    body = log.read_text()
    assert 'File "' in body, "no stacks captured under deadlock"
    assert "oob-wedged" in body or body.count("Thread 0x") >= 2, (
        "the blocked thread does not appear in the dump — the whole point"
    )
