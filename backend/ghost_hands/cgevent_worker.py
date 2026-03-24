#!/usr/bin/env python3
"""
CGEvent Worker -- persistent subprocess for silent UI automation.

Reads JSON-line commands from stdin, executes via CoreGraphics CGEvent,
writes JSON-line results to stdout.  Imports Quartz ONCE at startup
(safe in a dedicated subprocess, unsafe in the main JARVIS process).

Protocol (one JSON object per line):

  stdin:  {"cmd":"click","x":500,"y":300}
  stdout: {"ok":true}

  stdin:  {"cmd":"key","name":"return"}
  stdout: {"ok":true}

  stdin:  {"cmd":"type","text":"hello world"}
  stdout: {"ok":true}

  stdin:  {"cmd":"scroll","amount":-3,"x":500,"y":300}
  stdout: {"ok":true}

  stdin:  {"cmd":"ping"}
  stdout: {"ok":true,"pid":12345}

Run:  python3 backend/ghost_hands/cgevent_worker.py
"""
from __future__ import annotations

import json
import os
import sys
import time

# ------------------------------------------------------------------
# Key name → macOS virtual keycode mapping
# ------------------------------------------------------------------
_KEYCODES = {
    "return": 36, "enter": 36,
    "tab": 48,
    "space": 49,
    "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53,
    "command": 55, "cmd": 55,
    "shift": 56,
    "capslock": 57,
    "option": 58, "alt": 58,
    "control": 59, "ctrl": 59,
    "right_shift": 60,
    "right_option": 61,
    "right_control": 62,
    "fn": 63,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118,
    "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "up": 126, "down": 125, "left": 123, "right": 124,
    "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121,
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3,
    "g": 5, "h": 4, "i": 34, "j": 38, "k": 40, "l": 37,
    "m": 46, "n": 45, "o": 31, "p": 35, "q": 12, "r": 15,
    "s": 1, "t": 17, "u": 32, "v": 9, "w": 13, "x": 7,
    "y": 16, "z": 6,
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21,
    "5": 23, "6": 22, "7": 26, "8": 28, "9": 25,
}


def _respond(obj: dict) -> None:
    """Write a JSON-line response to stdout and flush."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _do_click(x: float, y: float) -> dict:
    """Post a click event at (x, y) via CGEvent."""
    import Quartz

    point = Quartz.CGPointMake(x, y)

    # Mouse move to target (avoids stale cursor position issues)
    move = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, point, Quartz.kCGMouseButtonLeft,
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    time.sleep(0.02)

    # Mouse down
    down = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseDown, point, Quartz.kCGMouseButtonLeft,
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    time.sleep(0.02)

    # Mouse up
    up = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseUp, point, Quartz.kCGMouseButtonLeft,
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

    return {"ok": True}


def _do_key(name: str) -> dict:
    """Press and release a key by name."""
    import Quartz

    keycode = _KEYCODES.get(name.lower())
    if keycode is None:
        return {"ok": False, "error": f"Unknown key: {name}"}

    # Key down
    down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    time.sleep(0.02)

    # Key up
    up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

    return {"ok": True}


def _do_type(text: str) -> dict:
    """Type text via clipboard paste (Cmd+V)."""
    import subprocess

    # Copy to clipboard
    proc = subprocess.run(
        ["pbcopy"], input=text.encode("utf-8"),
        capture_output=True, timeout=3,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": f"pbcopy failed: {proc.returncode}"}

    time.sleep(0.05)

    # Cmd+V via CGEvent
    import Quartz

    v_keycode = _KEYCODES["v"]  # 9

    down = Quartz.CGEventCreateKeyboardEvent(None, v_keycode, True)
    Quartz.CGEventSetFlags(down, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)

    time.sleep(0.02)

    up = Quartz.CGEventCreateKeyboardEvent(None, v_keycode, False)
    Quartz.CGEventSetFlags(up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

    return {"ok": True}


def _do_scroll(amount: int, x: float = 0, y: float = 0) -> dict:
    """Scroll at position."""
    import Quartz

    if x > 0 and y > 0:
        # Move mouse to position first
        point = Quartz.CGPointMake(x, y)
        move = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, point, Quartz.kCGMouseButtonLeft,
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
        time.sleep(0.02)

    scroll = Quartz.CGEventCreateScrollWheelEvent(
        None, Quartz.kCGScrollEventUnitLine, 1, int(amount),
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, scroll)

    return {"ok": True}


def main() -> None:
    """Main loop: read commands from stdin, execute, write results."""
    # Pre-import Quartz to pay the startup cost once
    try:
        import Quartz  # noqa: F401
        _respond({"ok": True, "status": "ready", "pid": os.getpid()})
    except ImportError as e:
        _respond({"ok": False, "status": "error", "error": f"Quartz import failed: {e}"})
        sys.exit(1)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            cmd = json.loads(line)
        except json.JSONDecodeError as e:
            _respond({"ok": False, "error": f"Invalid JSON: {e}"})
            continue

        action = cmd.get("cmd", "")

        try:
            if action == "click":
                result = _do_click(cmd.get("x", 0), cmd.get("y", 0))
            elif action == "key":
                result = _do_key(cmd.get("name", ""))
            elif action == "type":
                result = _do_type(cmd.get("text", ""))
            elif action == "scroll":
                result = _do_scroll(
                    cmd.get("amount", -3),
                    cmd.get("x", 0), cmd.get("y", 0),
                )
            elif action == "ping":
                result = {"ok": True, "pid": os.getpid()}
            else:
                result = {"ok": False, "error": f"Unknown command: {action}"}
        except Exception as e:
            result = {"ok": False, "error": str(e)}

        _respond(result)


if __name__ == "__main__":
    main()
