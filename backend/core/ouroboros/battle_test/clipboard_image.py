"""A screenshot on the clipboard becomes something the organism can read.

Dragging a file into the terminal already works — `drop_translate` turns the
injected path into `@relative/path` or `/attach /abs/path`. But the most
common way an operator has an image is not a file: it is `Cmd+Shift+Ctrl+4`,
a screenshot of the thing they are asking about, sitting on the clipboard and
never written to disk. Pasting it produced nothing at all, because a terminal
pastes TEXT and there is no text to paste.

So the clipboard is read, and if it holds an image it is spilled to a file —
at which point the problem is already solved. The path goes through the exact
`/attach` verb a dragged file uses, and everything downstream (validation,
the 10 MiB cap, sha256, the multi-modal GENERATE path) is the machinery that
already exists.

Reading it without a new dependency
-----------------------------------
`pngpaste` is the usual answer and is not installed. `osascript` is, on every
macOS box, and AppleScript can both interrogate the clipboard's type and
write its PNG payload. That is worth more than a nicer API: a paste feature
that requires `brew install` before it works is a paste feature most people
never turn on.

The type is CHECKED first, not inferred from a failed read. Asking for
`«class PNGf»` when the clipboard holds text produces an error that is
indistinguishable from a real failure, and the common case — the operator
pasting ordinary text — must be silent and instant, not an exception path.

Spilled, not held
-----------------
The bytes go to a real file under the system temp directory rather than being
carried in memory to the daemon. The UDS bridge is one ordered stream shared
with op chrome and the token mirror; a megabyte of base64 would block every
one of them behind it. The daemon reads files by path and is the process that
owns the repo, so a path is smaller and more truthful than a copy — the same
reasoning `drop_translate` records for dropped files.

Files are named by CONTENT hash, so pasting the same screenshot twice does
not accumulate copies, and are left for the OS to reap: deleting one while
the daemon is still reading it would be a race with no upside.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("Ouroboros.ClipboardImage")

__all__ = ["clipboard_paste_enabled", "clipboard_has_image",
           "spill_clipboard_image", "install_image_paste_binding"]

#: AppleScript, not a shell pipeline: the clipboard is not addressable from
#: the shell without a helper binary, and `osascript` ships with the OS.
_TYPE_SCRIPT = "return (clipboard info) as string"
_READ_SCRIPT = (
    'set p to (the clipboard as «class PNGf»)\n'
    'set f to open for access POSIX file "{path}" with write permission\n'
    'set eof f to 0\n'
    'write p to f\n'
    'close access f\n'
)

#: Beyond this a paste is not a screenshot. Matches the attachment cap the
#: GENERATE path already enforces, so a file that passes here cannot be
#: rejected downstream for size — one limit, stated once.
_MAX_BYTES = 10 * 1024 * 1024


def clipboard_paste_enabled() -> bool:
    """Default ON. Off, Ctrl+V is left to the terminal."""
    return os.environ.get(
        "JARVIS_CLIPBOARD_IMAGE_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def _osascript(script: str, timeout: float = 2.0) -> Optional[str]:
    """Run one AppleScript. None on any failure. NEVER raises.

    Timed out because a paste happens on the operator's keystroke: an
    AppleScript that blocks would freeze the prompt, which is a worse outcome
    than a paste that does not work.
    """
    try:
        proc = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        return proc.stdout if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def clipboard_has_image() -> bool:
    """Does the clipboard hold an image right now?

    Checked BEFORE reading. Asking for the PNG payload of a text clipboard
    produces an error indistinguishable from a real failure, and the common
    case — pasting ordinary text — has to be silent and instant rather than
    an exception path.
    """
    try:
        info = _osascript(_TYPE_SCRIPT)
        if not info:
            return False
        lowered = info.lower()
        return any(tag in lowered for tag in
                   ("pngf", "tiff", "jpeg", "picture", "«class png»"))
    except Exception:  # noqa: BLE001
        return False


def spill_clipboard_image() -> Optional[Path]:
    """Write the clipboard image to a file and return its path, or None.

    Named by CONTENT hash so pasting the same screenshot twice reuses one
    file instead of accumulating copies. Left for the OS to reap — deleting
    it while the daemon may still be reading it is a race with no upside.
    """
    try:
        if not clipboard_paste_enabled() or not clipboard_has_image():
            return None
        root = Path(tempfile.gettempdir()) / "ov-clipboard"
        root.mkdir(parents=True, exist_ok=True)
        staging = root / f"staging-{os.getpid()}.png"
        if _osascript(_READ_SCRIPT.format(path=str(staging)), timeout=5.0) is None:
            staging.unlink(missing_ok=True)
            return None
        if not staging.exists() or staging.stat().st_size == 0:
            staging.unlink(missing_ok=True)
            return None
        if staging.stat().st_size > _MAX_BYTES:
            # Refused HERE rather than downstream: a file that passes this
            # gate must not be rejected later for size, or the operator gets
            # a failure two layers from the paste that caused it.
            logger.debug("[ClipboardImage] refused: %d bytes",
                         staging.stat().st_size)
            staging.unlink(missing_ok=True)
            return None
        digest = hashlib.sha256(staging.read_bytes()).hexdigest()[:16]
        final = root / f"paste-{digest}.png"
        if final.exists():
            staging.unlink(missing_ok=True)
        else:
            staging.replace(final)
        return final
    except Exception:  # noqa: BLE001 — a paste must never crash the prompt
        logger.debug("[ClipboardImage] spill degraded", exc_info=True)
        return None


def install_image_paste_binding(kb: object, insert: object) -> bool:
    """Bind Ctrl+V to paste an image as `/attach <path>`. NEVER raises.

    Falls through to the terminal's own paste when the clipboard holds text,
    so the binding cannot break the ordinary case it sits in front of. That
    fall-through is the whole safety property: an image paste that swallowed
    text pastes would be worse than no image paste at all.
    """
    try:
        if kb is None or not clipboard_paste_enabled():
            return False

        @kb.add("c-v")  # type: ignore[union-attr]
        def _paste(event: object) -> None:
            try:
                path = spill_clipboard_image()
                if path is None:
                    # Ordinary text: hand it straight back to the buffer's
                    # own paste so nothing about typing changes.
                    buf = getattr(event, "current_buffer", None)
                    data = getattr(getattr(event, "app", None), "clipboard", None)
                    if buf is not None and data is not None:
                        buf.paste_clipboard_data(data.get_data())
                    return
                insert(f"/attach {path}")  # type: ignore[operator]
            except Exception:  # noqa: BLE001
                logger.debug("[ClipboardImage] paste degraded", exc_info=True)

        return True
    except Exception:  # noqa: BLE001
        logger.debug("[ClipboardImage] install degraded", exc_info=True)
        return False
