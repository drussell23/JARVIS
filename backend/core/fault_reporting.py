"""
fault_reporting - one place that arms Python's faulthandler for this process.

Why the Windows setting differs
-------------------------------
On Windows, ``faulthandler.enable()`` installs a *vectored* exception handler.
It runs before any ``__try``/``except`` gets a chance, for every SEH exception
whose code has the error bit set -- including the ones a library raises and
handles itself. COM does exactly that: a call to an object whose server went
away raises ``RPC_E_DISCONNECTED`` (0x80010108), and the caller (comtypes,
ctypes) turns it into an ordinary Python exception and carries on.

With ``all_threads=True`` (the default) each of those handled exceptions made
faulthandler walk EVERY thread's frames from inside the handler, without the
GIL, while those threads kept running. On 2026-10-15 12:09 the kernel logged
"Windows fatal exception: code 0x80010108", began dumping, and died mid-line
with an access violation -- it read a frame another thread was changing (the
dump shows ``line 1853140992``). A diagnostic killed the process it was
diagnosing. Reproduced: a handled ``RaiseException(0x80010108)`` dumps every
running thread with ``all_threads=True`` and only the raising thread without.

So on Windows the handler dumps only the thread that raised -- which is the
thread a real crash needs, read safely because it is the one executing the
handler. All-thread snapshots of a wedged loop are StallSampler's job
(backend/hud/stall_sampler.py), taken with the GIL held. Elsewhere faulthandler
is only reached by fatal signals, and all threads are dumped as before.
``JARVIS_FAULTHANDLER_ALL_THREADS`` overrides the platform default.

Idempotent; every caller (the kernel's entry point, memory_fault_guard) arms it
here so the last call cannot silently change the setting. NEVER raises.
"""
from __future__ import annotations

import faulthandler
import os
import sys
from typing import Optional, TextIO

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


def dump_all_threads() -> bool:
    """Whether the fault handler dumps every thread (see module docstring)."""
    raw = (os.environ.get("JARVIS_FAULTHANDLER_ALL_THREADS", "") or "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return sys.platform != "win32"


def enable_fault_reporting(file: Optional[TextIO] = None) -> bool:
    """Arm faulthandler (and, where the OS has it, a SIGUSR1 all-thread dump
    an operator can ask for). True when armed. NEVER raises."""
    try:
        kwargs = {"all_threads": dump_all_threads()}
        if file is not None:
            kwargs["file"] = file
        faulthandler.enable(**kwargs)
    except Exception:  # noqa: BLE001 -- no stderr fileno (pythonw), closed stream
        return False
    try:
        import signal

        if hasattr(signal, "SIGUSR1"):
            # Operator-requested (kill -USR1 <pid>): every thread, by request.
            faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    except Exception:  # noqa: BLE001
        pass
    return True


__all__ = ["dump_all_threads", "enable_fault_reporting"]
