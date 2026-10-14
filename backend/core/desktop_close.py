"""
desktop_close - close an application, a window or an open file on the user's desktop.

The counterpart of desktop_open. A close is GRACEFUL: the program is asked
to close the way the user's own click on its X would ask (it can save, or
ask "save changes?"), never killed. What is closed is decided from what is
on the screen right now, never from a table of names in code:

* Windows -- the visible top-level windows (desktop_open's own enumeration),
  each known by its program's executable name, the product name the program
  declares about itself ("Microsoft Word" for WINWORD.EXE) and its title.
  A name that matches a PROGRAM closes all of that program's windows ("close
  firefox"); otherwise a name that matches a window's TITLE closes that
  window ("close notes.txt", "close my downloads folder"). Each gets
  WM_CLOSE, then the close is verified: a window still there after
  ``JARVIS_CLOSE_WAIT_S`` is reported, not forced.
* macOS -- the AppleScript quit JARVIS already has
  (system_control.app_closer_enhanced).
* Linux -- the window manager's graceful close (``wmctrl -c``), else SIGTERM
  by process name.

Names are matched with desktop_open.best_match, the same adaptive matching
opening uses. JARVIS never closes the windows it runs in: a window owned by
this process, one of its ancestors (the terminal it was started from) or a
Trinity-owned process is left alone and the reply says so. Every entry point
is async and never raises.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Set, Tuple

from backend.core.desktop_open import _env_float, _norm, _run, best_match

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"


def _close_wait_s() -> float:
    """How long closed windows may take to go (a save prompt keeps them)."""
    return _env_float("JARVIS_CLOSE_WAIT_S", 5.0)


def _close_poll_s() -> float:
    return _env_float("JARVIS_CLOSE_POLL_S", 0.1)


@dataclass(frozen=True)
class CloseOutcome:
    ok: bool
    target: str                                  # what was asked for
    closed: Tuple[str, ...] = ()                 # windows / programs that went
    still_open: Tuple[str, ...] = ()             # asked, but still there
    spared: Tuple[str, ...] = ()                 # JARVIS's own, left alone
    method: str = ""
    error: str = ""

    def describe(self) -> str:
        parts: List[str] = []
        if self.closed:
            parts.append(f"Closed {_listing(self.closed)}")
        if self.still_open:
            parts.append(f"{_listing(self.still_open)} is still open -- it may be "
                         "asking whether to save")
        if self.spared:
            parts.append(f"left {_listing(self.spared)} open: JARVIS is running in it")
        if not parts:
            parts.append(f"Could not close {self.target}: {self.error or 'nothing open matches'}")
        return "; ".join(parts) + "."


def _listing(items: Sequence[str]) -> str:
    items = list(dict.fromkeys(i for i in items if i))
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

@dataclass
class _Window:
    hwnd: int
    title: str
    exe: str
    pid: int
    program: str = ""                            # the product name it declares
    names: Set[str] = field(default_factory=set)


def _win_window_pid(hwnd: int) -> int:
    import ctypes
    from ctypes import wintypes

    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _win_exe_path(pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return buf.value if ok else ""
    finally:
        kernel32.CloseHandle(h)


def _win_product_name(exe_path: str) -> str:
    """The FileDescription (else ProductName) an executable declares, "" if none."""
    import ctypes
    from ctypes import wintypes

    if not exe_path:
        return ""
    version = ctypes.windll.version
    size = version.GetFileVersionInfoSizeW(exe_path, None)
    if not size:
        return ""
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(exe_path, 0, size, data):
        return ""
    trans = ctypes.c_void_p()
    trans_len = wintypes.UINT()
    if not version.VerQueryValueW(data, "\\VarFileInfo\\Translation",
                                  ctypes.byref(trans), ctypes.byref(trans_len)) or not trans_len.value:
        return ""
    lang, codepage = ctypes.cast(trans, ctypes.POINTER(wintypes.WORD * 2)).contents
    for key in ("FileDescription", "ProductName"):
        value = ctypes.c_wchar_p()
        value_len = wintypes.UINT()
        sub = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{key}"
        if version.VerQueryValueW(data, sub, ctypes.byref(value), ctypes.byref(value_len)) \
                and value_len.value and value.value and value.value.strip():
            return value.value.strip()
    return ""


def _win_windows() -> List[_Window]:
    from backend.core.desktop_open import _win_top_windows

    products: Dict[int, str] = {}
    windows: List[_Window] = []
    for hwnd, title, exe in _win_top_windows():
        pid = _win_window_pid(hwnd)
        if pid not in products:
            products[pid] = _win_product_name(_win_exe_path(pid))
        stem = os.path.splitext(exe)[0]
        windows.append(_Window(hwnd, title, exe, pid, products[pid] or stem,
                               {n for n in (stem, products[pid]) if n}))
    return windows


def _protected_pids() -> Set[int]:
    """This process and its ancestors: the windows JARVIS itself runs in."""
    pids = {os.getpid()}
    try:
        import psutil
        proc = psutil.Process()
        pids.update(p.pid for p in proc.parents())
    except Exception as exc:  # noqa: BLE001 -- without psutil only this process is known
        logger.debug("[DesktopClose] ancestry unavailable: %s", exc)
    return pids


def _is_jarvis(window: _Window, protected: Set[int]) -> bool:
    """A window JARVIS runs in or depends on: this process, the terminal it
    was started from, or a process running Trinity's own code (J-Prime,
    Reactor). NOT "anything JARVIS started" -- an app JARVIS opened for the
    user is the user's to close."""
    if window.pid in protected:
        return True
    try:
        import psutil
        from backend.core.process_identity import is_trinity_process
        return is_trinity_process(psutil.Process(window.pid))
    except Exception:  # noqa: BLE001 -- unknown: not JARVIS's by evidence
        return False


def _choose_windows(target: str, windows: List[_Window]) -> Tuple[List[_Window], str]:
    """The windows *target* names: every window of the program it names, else
    the one window whose title it names. Returns (windows, "program"|"window")."""
    program_names = sorted({n for w in windows for n in w.names})
    program = best_match(target, program_names)
    if program is not None:
        wanted = _norm(program)
        return [w for w in windows if wanted in {_norm(n) for n in w.names}], "program"
    title = best_match(target, [w.title for w in windows])
    if title is not None:
        return [w for w in windows if w.title == title], "window"
    return [], ""


async def _close_windows(target: str) -> CloseOutcome:
    import ctypes

    user32 = ctypes.windll.user32
    windows = await asyncio.to_thread(_win_windows)
    chosen, how = _choose_windows(target, windows)
    if not chosen:
        return CloseOutcome(False, target, error="nothing open matches it")
    protected = await asyncio.to_thread(_protected_pids)
    spared = [w for w in chosen if _is_jarvis(w, protected)]
    asked = [w for w in chosen if w not in spared]
    for w in asked:
        user32.PostMessageW(w.hwnd, 0x0010, 0, 0)  # WM_CLOSE: what the window's X sends
    deadline = time.monotonic() + _close_wait_s()
    remaining = list(asked)
    while remaining and time.monotonic() < deadline:
        await asyncio.sleep(_close_poll_s())
        remaining = [w for w in remaining if user32.IsWindow(w.hwnd)]

    def label(w: _Window) -> str:
        return w.program if how == "program" else w.title

    closed = tuple(dict.fromkeys(label(w) for w in asked if w not in remaining))
    return CloseOutcome(
        ok=bool(closed) and not remaining,
        target=target,
        closed=closed,
        still_open=tuple(dict.fromkeys(label(w) for w in remaining)),
        spared=tuple(dict.fromkeys(label(w) for w in spared)),
        method="wm_close",
    )


# ---------------------------------------------------------------------------
# macOS / Linux
# ---------------------------------------------------------------------------

async def _close_macos(target: str) -> CloseOutcome:
    from backend.system_control.app_closer_enhanced import close_app_enhanced

    ok, message = await asyncio.to_thread(close_app_enhanced, target)
    return CloseOutcome(ok, target, closed=(target,) if ok else (),
                        method="applescript_quit", error="" if ok else message)


async def _close_linux(target: str) -> CloseOutcome:
    timeout = _close_wait_s()
    if shutil.which("wmctrl"):
        rc, out = await _run(["wmctrl", "-c", target], timeout)
        if rc == 0:
            return CloseOutcome(True, target, closed=(target,), method="wmctrl")
    rc, out = await _run(["pkill", "-TERM", "-i", "-x", target], timeout)
    if rc == 0:
        return CloseOutcome(True, target, closed=(target,), method="sigterm")
    return CloseOutcome(False, target, error=out.strip()[:120] or "nothing open matches it")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def open_windows() -> List[Tuple[str, str]]:
    """(program, title) of what is open on the desktop now, top window first:
    the facts a request to close something is decided against. Empty where
    the platform's windows are not enumerated (the request's own words then
    name the target)."""
    if not _IS_WINDOWS:
        return []
    try:
        return [(w.program, w.title) for w in await asyncio.to_thread(_win_windows)]
    except Exception as exc:  # noqa: BLE001
        logger.debug("[DesktopClose] windows unreadable: %s", exc)
        return []


async def close_target(target: str) -> CloseOutcome:
    """Gracefully close what *target* names: a program, a window or a file."""
    target = (target or "").strip()
    if not target:
        return CloseOutcome(False, target, error="nothing was named to close")
    try:
        if _IS_WINDOWS:
            return await _close_windows(target)
        if _IS_MACOS:
            return await _close_macos(target)
        return await _close_linux(target)
    except Exception as exc:  # noqa: BLE001
        return CloseOutcome(False, target, error=f"{type(exc).__name__}: {exc}")


__all__ = ["CloseOutcome", "close_target", "open_windows"]
