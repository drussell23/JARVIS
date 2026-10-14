"""
browser_tabs - reuse JARVIS's own browser tab instead of opening another.

On macOS the supervisor's AppleScript manager finds the tab already showing
JARVIS and points it at the new page. Elsewhere every boot opened a new tab
(a URL handed to the shell always opens one), so each start added a
"J.A.R.V.I.S. Interface" tab -- and every one of them connected to the backend
and played JARVIS's speech.

Windows equivalent, through UI Automation (no browser flags, no extension):

1. look only in the browser URLs open in (desktop_open: JARVIS_DEFAULT_BROWSER
   or the system's https handler);
2. candidates are tabs titled like one of JARVIS's own pages (the caller
   passes the titles its pages declare);
3. a candidate is JARVIS's only if the address bar, once the tab is
   selected, shows one of JARVIS's own origins (loopback + its ports);
4. that tab is pointed at the new URL, and the navigation is VERIFIED (the
   address bar gives up focus as the page commits, and the tab takes the
   expected title) -- anything less reports "not reused" so the caller opens
   a tab as before;
5. other JARVIS tabs are closed (JARVIS_BROWSER_CLOSE_DUPLICATE_TABS),
   as the macOS manager closes its duplicates.

All COM work stays on ONE worker thread and is released there (a COM object
collected on the event loop thread blocked boot once -- see the pyttsx3 note
in trinity_voice_coordinator).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}


@dataclass
class TabReuse:
    reused: bool
    closed: int = 0
    detail: str = ""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _close_duplicates() -> bool:
    return os.environ.get("JARVIS_BROWSER_CLOSE_DUPLICATE_TABS", "true").strip().lower() in (
        "1", "true", "yes", "on")


def is_own_address(address: str, ports: Iterable[int]) -> bool:
    """True when *address* (as an address bar shows it, scheme optional) is
    one of JARVIS's own pages: a loopback host on one of *ports*."""
    text = (address or "").strip()
    if not text:
        return False
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return False
    return host in _LOOPBACK and port in set(ports)


def titled_like(name: str, titles: Sequence[str]) -> bool:
    """A tab name carries one of JARVIS's page titles (pages prefix state
    glyphs, the browser appends memory/audio annotations)."""
    return any(t and t in (name or "") for t in titles)


async def reuse_tab(url: str, *, page_titles: Sequence[str], ports: Iterable[int],
                    expected_title: str = "") -> TabReuse:
    """Point JARVIS's existing tab at *url*; TabReuse.reused is False when
    there is none (or this OS has no such mechanism here) -- open one then."""
    if sys.platform != "win32":
        return TabReuse(False, detail="no tab automation on this OS")
    from backend.core.desktop_open import _load_browsers, _command_argv, _win_url_handler_exe, best_match, preferred_browser

    browser_exe = ""
    named = preferred_browser()
    if named:
        browsers = await _load_browsers()
        match = best_match(named, list(browsers))
        if match:
            argv = _command_argv(browsers[match])
            browser_exe = os.path.basename(argv[0]).lower() if argv else ""
    if not browser_exe:
        browser_exe = await asyncio.to_thread(_win_url_handler_exe)
    if not browser_exe:
        return TabReuse(False, detail="no browser to look in")
    return await asyncio.to_thread(
        _reuse_tab_win, url, list(page_titles), sorted(set(ports)), browser_exe, expected_title)


def _reuse_tab_win(url: str, titles: List[str], ports: List[int], browser_exe: str,
                   expected_title: str) -> TabReuse:
    import comtypes
    import comtypes.client

    comtypes.CoInitialize()
    try:
        return _reuse_tab_com(url, titles, ports, browser_exe, expected_title)
    except Exception as exc:  # noqa: BLE001 -- any UIA failure: the caller opens a tab
        logger.debug("[BrowserTabs] reuse failed: %s", exc, exc_info=True)
        return TabReuse(False, detail=f"{type(exc).__name__}: {exc}")
    finally:
        import gc
        gc.collect()  # release every COM object on this thread, before uninitialising
        comtypes.CoUninitialize()


def _reuse_tab_com(url: str, titles: List[str], ports: List[int], browser_exe: str,
                   expected_title: str) -> TabReuse:
    import comtypes.client

    from backend.core.desktop_open import _win_bring_forward, _win_top_windows

    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as U

    uia = comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
    tab_cond = uia.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_TabItemControlTypeId)
    edit_cond = uia.CreateAndCondition(
        uia.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_EditControlTypeId),
        uia.CreatePropertyCondition(U.UIA_IsValuePatternAvailablePropertyId, True))
    button_cond = uia.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_ButtonControlTypeId)
    poll = _env_float("JARVIS_BROWSER_TAB_POLL_S", 0.1)
    settle = _env_float("JARVIS_BROWSER_TAB_SETTLE_S", 1.5)
    commit = _env_float("JARVIS_BROWSER_TAB_COMMIT_S", 8.0)

    def pattern(element, pattern_id, interface):
        return element.GetCurrentPattern(pattern_id).QueryInterface(interface)

    def address_of(window) -> tuple:
        bar = window.FindFirst(U.TreeScope_Descendants, edit_cond)
        if not bar:
            return None, ""
        return bar, pattern(bar, U.UIA_ValuePatternId, U.IUIAutomationValuePattern).CurrentValue or ""

    candidates = []  # (hwnd, window element, tab element)
    for hwnd, _title, exe in _win_top_windows():
        if exe != browser_exe:
            continue
        window = uia.ElementFromHandle(hwnd)
        tabs = window.FindAll(U.TreeScope_Descendants, tab_cond)
        for i in range(tabs.Length):
            tab = tabs.GetElement(i)
            if titled_like(tab.CurrentName, titles):
                candidates.append((hwnd, window, tab))
    if not candidates:
        return TabReuse(False, detail="no JARVIS tab open")

    chosen = None
    for hwnd, window, tab in candidates:
        pattern(tab, U.UIA_SelectionItemPatternId, U.IUIAutomationSelectionItemPattern).Select()
        # The address bar follows the selected tab once the strip updates.
        deadline = time.monotonic() + settle
        bar, address = address_of(window)
        while not is_own_address(address, ports) and time.monotonic() < deadline:
            time.sleep(poll)
            bar, address = address_of(window)
        if bar is not None and is_own_address(address, ports):
            chosen = (hwnd, window, tab, bar)
            break
    if chosen is None:
        return TabReuse(False, detail="tabs titled like JARVIS show other addresses")

    hwnd, window, tab, bar = chosen
    if not _win_bring_forward(hwnd):
        return TabReuse(False, detail="browser window could not take the foreground")
    pattern(bar, U.UIA_ValuePatternId, U.IUIAutomationValuePattern).SetValue(url)
    bar.SetFocus()
    if not uia.CompareElements(uia.GetFocusedElement(), bar):
        return TabReuse(False, detail="address bar did not take focus")
    _press_enter()

    # Committed navigation moves focus from the address bar into the page;
    # the tab then carries the new page's title.
    deadline = time.monotonic() + commit
    committed = False
    while time.monotonic() < deadline:
        if not bar.CurrentHasKeyboardFocus and (
                not expected_title or expected_title in (tab.CurrentName or "")):
            committed = True
            break
        time.sleep(poll)
    if not committed:
        # The page may be slow to take its title (a busy boot) while the
        # navigation has already happened: focus has left the address bar and
        # the bar shows the new page's origin. Typed-but-unsubmitted text keeps
        # the focus, so it never counts. Reporting "not reused" here made the
        # caller open a second tab beside the one just navigated.
        _bar, shown = address_of(window)
        committed = not bar.CurrentHasKeyboardFocus and is_own_address(shown, [urlsplit(url).port])
    if not committed:
        return TabReuse(False, detail="navigation in the existing tab was not confirmed")

    closed = 0
    if _close_duplicates():
        for _hwnd, _window, other in candidates:
            if uia.CompareElements(other, tab):
                continue
            close = other.FindFirst(U.TreeScope_Children, button_cond)
            if close:
                pattern(close, U.UIA_InvokePatternId, U.IUIAutomationInvokePattern).Invoke()
                closed += 1
    return TabReuse(True, closed=closed, detail="reused")


def _press_enter() -> None:
    """One Enter keystroke to the focused control (the address bar)."""
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        # MOUSEINPUT is the largest member; its size fixes INPUT's.
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    vk_return, keyup, input_keyboard = 0x0D, 0x0002, 1
    events = (INPUT * 2)(INPUT(input_keyboard, _U(ki=KEYBDINPUT(vk_return, 0, 0, 0, 0))),
                         INPUT(input_keyboard, _U(ki=KEYBDINPUT(vk_return, 0, keyup, 0, 0))))
    ctypes.windll.user32.SendInput(2, events, ctypes.sizeof(INPUT))


__all__ = ["TabReuse", "is_own_address", "reuse_tab", "titled_like"]
