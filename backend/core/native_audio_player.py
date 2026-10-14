"""
native_audio_player - play a synthesized audio file with the OS's own player.

The voice layer plays TTS output OUTSIDE the Python audio stack on purpose:
a PortAudio callback needs the GIL every ~20 ms, so GIL-heavy work turned
speech into static (v283.0). Each platform's native path keeps that property:

* macOS  - ``afplay`` subprocess.
* Windows - the MCI API in ``winmm`` (``open ... type mpegvideo`` handles MP3
  and WAV), called through ctypes in a worker thread; ctypes releases the GIL
  for the duration of the blocking ``play ... wait`` call. No dependency, no
  subprocess. Under Remote Desktop the session's audio redirection carries it
  to the client machine.
* other  - ``mpg123``.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
from typing import List, Optional

_mci_lock = threading.Lock()
_mci_seq = 0


def _mci(command: str) -> int:
    import ctypes

    buf = ctypes.create_unicode_buffer(256)
    return ctypes.windll.winmm.mciSendStringW(command, buf, len(buf), None)


def _mci_play_blocking(path: str) -> None:
    global _mci_seq
    with _mci_lock:
        _mci_seq += 1
        alias = f"jarvis_tts_{os.getpid()}_{_mci_seq}"
    err = _mci(f'open "{path}" type mpegvideo alias {alias}')
    if err:
        raise OSError(f"MCI open failed ({err}) for {path}")
    try:
        err = _mci(f"play {alias} wait")
        if err:
            raise OSError(f"MCI play failed ({err}) for {path}")
    finally:
        _mci(f"close {alias}")


def player_command(path: str) -> Optional[List[str]]:
    """The external player argv for ``path``, or None where playback is in-process."""
    if sys.platform == "win32":
        return None
    if sys.platform == "darwin" or shutil.which("afplay"):
        return ["afplay", path]
    return ["mpg123", "-q", path]


async def play_audio_file(path: str, timeout: Optional[float] = None) -> None:
    """Play ``path`` to completion on the native player. Raises on failure."""
    argv = player_command(path)
    if argv is None:
        await asyncio.wait_for(asyncio.to_thread(_mci_play_blocking, path), timeout=timeout)
        return
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(process.wait(), timeout=timeout)


__all__ = ["play_audio_file", "player_command"]
