"""What desktop_open opens is brought in front, or the outcome says it is not.

Everything after an open acts on the foreground window; on Windows a program
started by a background process is not given it (the vision loop once typed
a search into the terminal that stayed in front).
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core import desktop_open as d

TERMINAL, BROWSER, OTHER = 1, 2, 3


def _scripted(monkeypatch, frames, front=TERMINAL, bring_ok=True):
    """frames: successive _win_top_windows() results (first = before the open)."""
    seq = iter(frames)
    last = {}

    def top():
        nonlocal last
        last = next(seq, last)
        return last

    brought = []
    monkeypatch.setattr(d, "_IS_WINDOWS", True)
    monkeypatch.setattr(d, "_win_top_windows", top)
    monkeypatch.setattr(d, "_win_foreground", lambda: front)
    monkeypatch.setattr(d, "_win_bring_forward", lambda h: brought.append(h) or bring_ok)
    monkeypatch.setenv("JARVIS_OPEN_WINDOW_WAIT_S", "0.5")
    monkeypatch.setenv("JARVIS_OPEN_WINDOW_POLL_S", "0.01")
    return brought


async def _ok():
    return d.OpenOutcome(True, "url", "https://x", "https://x", "shell")


def test_the_handlers_retitled_window_is_brought_forward_even_if_it_was_in_front(monkeypatch):
    # The user typed the command into the browser: the browser IS the front
    # window and is the one the URL retitles. The terminal spins meanwhile.
    before = [(TERMINAL, "⠋ claude", "windowsterminal.exe"), (BROWSER, "JARVIS", "chrome.exe")]
    after = [(TERMINAL, "⠙ claude", "windowsterminal.exe"), (BROWSER, "dogs - Google", "chrome.exe")]
    brought = _scripted(monkeypatch, [before, after], front=BROWSER)
    out = asyncio.run(d._opened_in_front(_ok(), "chrome.exe"))
    assert brought == [BROWSER] and out.in_front is True and out.window == "dogs - Google"


def test_unknown_handler_never_picks_the_window_that_was_in_front(monkeypatch):
    before = [(TERMINAL, "⠋ claude", "windowsterminal.exe")]
    after = [(TERMINAL, "⠙ claude", "windowsterminal.exe"), (OTHER, "Untitled - Notepad", "notepad.exe")]
    brought = _scripted(monkeypatch, [before, after], front=TERMINAL)
    out = asyncio.run(d._opened_in_front(_ok()))
    assert brought == [OTHER] and out.window == "Untitled - Notepad"


def test_nothing_appearing_is_reported_not_assumed(monkeypatch):
    same = [(TERMINAL, "claude", "windowsterminal.exe")]
    brought = _scripted(monkeypatch, [same, same], front=TERMINAL)
    out = asyncio.run(d._opened_in_front(_ok(), "chrome.exe"))
    assert brought == [] and out.ok and out.in_front is False


def test_a_window_that_refuses_the_foreground_is_reported(monkeypatch):
    before = [(TERMINAL, "claude", "windowsterminal.exe")]
    after = before + [(BROWSER, "dogs", "chrome.exe")]
    _scripted(monkeypatch, [before, after], bring_ok=False)
    out = asyncio.run(d._opened_in_front(_ok(), "chrome.exe"))
    assert out.in_front is False and out.window == "dogs"


def test_failed_open_is_returned_untouched(monkeypatch):
    brought = _scripted(monkeypatch, [[], []])

    async def fail():
        return d.OpenOutcome(False, "app", "nope", error="not found")

    out = asyncio.run(d._opened_in_front(fail()))
    assert not out.ok and out.in_front is None and brought == []


@pytest.mark.parametrize("plat", [False])
def test_unobserved_platform_leaves_in_front_unknown(monkeypatch, plat):
    monkeypatch.setattr(d, "_IS_WINDOWS", plat)
    out = asyncio.run(d._opened_in_front(_ok()))
    assert out.in_front is None
