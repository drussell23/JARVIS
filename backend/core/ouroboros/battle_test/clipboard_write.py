"""Putting text on the operator's clipboard, and saying which way it went.

Nothing in this repo could write the clipboard. `clipboard_image` reads one
(and only on macOS, via `osascript`), and prompt_toolkit's own clipboard is
in-memory unless `pyperclip` is installed, which it is not. So the cockpit
could offer a selection and then have nowhere to put it.

Claude Code documents the cascade this implements, and the reason it is a
cascade rather than one call is that the answer genuinely differs by
environment:

    macOS: pbcopy. Linux: wl-copy on Wayland, or xclip or xsel on X11,
    whichever is installed … Windows and WSL: PowerShell Set-Clipboard.
    Inside tmux it also writes to the tmux paste buffer. Over SSH it falls
    back to OSC 52 escape sequences.

Why it reports the path it used
-------------------------------
CC "prints a toast after each copy telling you which path it used", and that
is not decoration. These paths fail differently and visibly: OSC 52 is
silently swallowed by terminals that block it, the tmux buffer is not the
system clipboard, and X11's PRIMARY selection pastes with middle-click rather
than with Cmd+V. An operator who is told "copied via OSC 52" can reason about
why their paste did not arrive. One told only "copied" cannot.

Why every writer is tried, not just the first that exists
---------------------------------------------------------
A tool being INSTALLED is not the same as it WORKING — `xclip` on a machine
with no `DISPLAY` exits non-zero, and `wl-copy` outside a Wayland session
does too. So a writer counts only when it exits cleanly, and the cascade
continues past one that claims to exist and then fails. Reporting success
because a binary was on PATH is how an operator ends up with an empty
clipboard and no idea why.

Never a shell
-------------
Every writer takes the text on STDIN and an argv LIST. The text is a
selection from a transcript that contains model-authored output; a shell
string would make every metacharacter in it executable, and this is the one
module whose whole job is to handle text somebody else wrote.
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ClipboardWrite")

CLIPBOARD_WRITE_SCHEMA_VERSION: str = "clipboard_write.1"

__all__ = [
    "CLIPBOARD_WRITE_SCHEMA_VERSION",
    "clipboard_write_enabled",
    "copy_text",
    "describe_path",
    "max_copy_chars",
    "osc52_limit",
]

#: Terminals commonly cap an OSC 52 payload; xterm's historical limit is the
#: one most others copied. Exceeded, the sequence is dropped SILENTLY — which
#: is why this is checked rather than attempted and hoped for.
_OSC52_DEFAULT_LIMIT = 74_994


def clipboard_write_enabled() -> bool:
    """``JARVIS_CLIPBOARD_WRITE_ENABLED`` (default true). NEVER raises."""
    return os.environ.get(
        "JARVIS_CLIPBOARD_WRITE_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def max_copy_chars() -> int:
    """Cap on one copy. NEVER raises.

    A selection is operator-driven, but "select all" over a 20 000-line
    canvas is one gesture, and piping megabytes through a subprocess on the
    render thread's behalf is how a copy becomes a freeze.
    """
    try:
        return max(1_000, min(20_000_000, int(
            os.environ.get("JARVIS_CLIPBOARD_MAX_CHARS", "") or 2_000_000)))
    except (TypeError, ValueError):
        return 2_000_000


def osc52_limit() -> int:
    """Largest OSC 52 payload worth attempting. NEVER raises."""
    try:
        return max(1_000, min(10_000_000, int(
            os.environ.get("JARVIS_OSC52_LIMIT", "") or _OSC52_DEFAULT_LIMIT)))
    except (TypeError, ValueError):
        return _OSC52_DEFAULT_LIMIT


def _in_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def _over_ssh() -> bool:
    return bool(os.environ.get("SSH_CONNECTION")
                or os.environ.get("SSH_TTY")
                or os.environ.get("SSH_CLIENT"))


def _wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def _x11() -> bool:
    return bool(os.environ.get("DISPLAY"))


def _run(argv: List[str], text: str, timeout: float = 3.0) -> bool:
    """One writer. True only on a CLEAN exit. NEVER raises.

    A non-zero exit is the whole point of checking: `xclip` with no DISPLAY
    and `wl-copy` outside a Wayland session both exist and both fail, and a
    cascade that stopped at "the binary is on PATH" would report success and
    leave the clipboard empty.
    """
    try:
        proc = subprocess.run(
            argv, input=text.encode("utf-8", "replace"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    except Exception:  # noqa: BLE001
        return False


def _native_writers() -> List[Tuple[str, List[str]]]:
    """Platform writers, in the order CC documents. NEVER raises.

    On X11 both the CLIPBOARD and the PRIMARY selection are written, because
    they are two different buffers and an operator who middle-clicks is
    reaching for PRIMARY. Writing only one makes the copy work with Ctrl+V
    and not with the middle button, which reads as an intermittent bug.
    """
    out: List[Tuple[str, List[str]]] = []
    try:
        if sys.platform == "darwin":
            if shutil.which("pbcopy"):
                out.append(("pbcopy", ["pbcopy"]))
            return out
        if sys.platform.startswith("win"):
            if shutil.which("powershell"):
                out.append(("Set-Clipboard",
                            ["powershell", "-NoProfile", "-Command",
                             "$input | Set-Clipboard"]))
            return out
        # Linux / BSD
        if _wayland() and shutil.which("wl-copy"):
            out.append(("wl-copy", ["wl-copy"]))
        if _x11():
            if shutil.which("xclip"):
                out.append(("xclip", ["xclip", "-selection", "clipboard"]))
                out.append(("xclip:primary",
                            ["xclip", "-selection", "primary"]))
            elif shutil.which("xsel"):
                out.append(("xsel", ["xsel", "--clipboard", "--input"]))
                out.append(("xsel:primary", ["xsel", "--primary", "--input"]))
        # WSL: a Linux userland with a Windows clipboard behind it.
        if shutil.which("clip.exe"):
            out.append(("clip.exe", ["clip.exe"]))
    except Exception:  # noqa: BLE001
        pass
    return out


def _write_osc52(text: str, out_stream: object = None) -> bool:
    """The escape-sequence path — the only one that works over SSH.

    Written to the REAL stdout by descriptor, for the same reason the
    alternate screen's restore is: `sys.stdout` may be a `patch_stdout` proxy
    or a Rich capture, and a control sequence delivered into a proxy never
    reaches the terminal.

    Oversized payloads are refused rather than attempted: past the terminal's
    limit the sequence is dropped SILENTLY, and a copy that reports success
    and did nothing is worse than one that says it could not.
    """
    try:
        # A TERMINAL, or nothing. An escape sequence written into a pipe, a
        # log or a captured test stream is not a copy — it is corruption of
        # whatever is reading that stream, and it is invisible until someone
        # greps the output and finds `]52;c;` glued to a line. Probed through
        # `real_stdout_isatty` because `sys.stdout.isatty()` is False under
        # the active `patch_stdout` proxy, which is the same trap the
        # presentation layer already documents.
        if out_stream is None:
            try:
                from backend.core.ouroboros.battle_test.presentation_restraint import (  # noqa: E501
                    real_stdout_isatty,
                )
                if not real_stdout_isatty():
                    return False
            except Exception:  # noqa: BLE001
                return False
        payload = text.encode("utf-8", "replace")
        if len(base64.b64encode(payload)) > osc52_limit():
            return False
        encoded = base64.b64encode(payload).decode("ascii")
        seq = f"\x1b]52;c;{encoded}\x07"
        if _in_tmux():
            # tmux swallows an unknown OSC unless it is wrapped for
            # passthrough, and `allow-passthrough` must be on for even this
            # to reach the outer terminal.
            seq = f"\x1bPtmux;\x1b{seq}\x1b\\"
        stream = out_stream if out_stream is not None else (
            sys.__stdout__ or sys.stdout)
        if stream is None:
            return False
        fd = stream.fileno()
        data = seq.encode("ascii", "ignore")
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                return False
            view = view[written:]
        return True
    except Exception:  # noqa: BLE001
        return False


def describe_path(path: str) -> str:
    """One operator-readable sentence about how a copy travelled.

    These paths fail differently, and naming the one used is what lets an
    operator reason about a paste that did not arrive.
    """
    return {
        "pbcopy": "copied",
        "Set-Clipboard": "copied",
        "clip.exe": "copied (Windows clipboard)",
        "wl-copy": "copied",
        "xclip": "copied",
        "xsel": "copied",
        "xclip:primary": "copied to the primary selection (middle-click)",
        "xsel:primary": "copied to the primary selection (middle-click)",
        "tmux": "copied to the tmux buffer (not the system clipboard)",
        "osc52": "copied via OSC 52 — some terminals block this",
    }.get(str(path), "copied")


def copy_text(text: object, *, out_stream: object = None) -> Optional[str]:
    """Put text on the clipboard. Returns the path used, or None.

    Tries every applicable writer and reports the FIRST that exited cleanly.
    Inside tmux the paste buffer is written as well as the system clipboard,
    because they are different buffers and an operator inside tmux reaches
    for both. NEVER raises: a failed copy is reported, never thrown at a
    render thread.
    """
    if not clipboard_write_enabled():
        return None
    try:
        payload = str(text or "")
        if not payload:
            return None
        cap = max_copy_chars()
        if len(payload) > cap:
            payload = payload[:cap]
            logger.debug("[Clipboard] truncated to %d chars", cap)

        used: Optional[str] = None
        for name, argv in _native_writers():
            if _run(argv, payload):
                # PRIMARY is written IN ADDITION to the clipboard, so it must
                # not be reported as the path when the real clipboard already
                # took it — the operator would be told to middle-click when
                # Ctrl+V works.
                if used is None or not name.endswith(":primary"):
                    used = used or name
                continue

        if _in_tmux() and shutil.which("tmux"):
            if _run(["tmux", "load-buffer", "-"], payload):
                used = used or "tmux"

        # OSC 52 last, and ALWAYS attempted over SSH: there is no local tool
        # that can reach the operator's actual machine from the far end.
        if used is None or _over_ssh():
            if _write_osc52(payload, out_stream=out_stream):
                used = used or "osc52"
        return used
    except Exception:  # noqa: BLE001
        logger.debug("[Clipboard] copy degraded", exc_info=True)
        return None
