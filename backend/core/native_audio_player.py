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

There is ONE speaker. Every caller -- the voice coordinator's parallel
workers, the orchestrator's fallback, narration -- plays through
:func:`play_audio_file`, and it plays one file at a time: synthesis may run in
parallel, playback never does. The coordinator ran several workers whose
playbacks overlapped, and the operator heard JARVIS talking over himself.
The lock is a thread lock held by the thread that plays, so it holds across
event loops and across the asyncio cancellation of a caller (a cancelled
caller's line still finishes, then releases the speaker).
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
_speaker = threading.Lock()


class SpeakerBusy(TimeoutError):
    """The speaker stayed busy past the caller's wait: the line is stale."""


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


def _play_exclusive(path: str, wait_s: Optional[float], timeout: Optional[float]) -> None:
    """Take the speaker (waiting at most *wait_s*; None = until free), play
    *path* to completion, release. Runs in a worker thread."""
    if not _speaker.acquire(timeout=-1 if wait_s is None else max(0.0, wait_s)):
        raise SpeakerBusy(f"speaker busy for {wait_s:.1f}s")
    try:
        argv = player_command(path)
        if argv is None:
            _mci_play_blocking(path)
        else:
            import subprocess

            subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=timeout, check=False)
    finally:
        _speaker.release()


async def play_audio_file(path: str, timeout: Optional[float] = None,
                          wait_s: Optional[float] = None) -> None:
    """Play ``path`` to completion on the native player, alone on the speaker.

    *timeout* bounds the playback; *wait_s* bounds the wait for the speaker
    (None: until it is free) -- a line that cannot start within it is stale
    and raises :class:`SpeakerBusy`. Raises on failure."""
    await asyncio.to_thread(_play_exclusive, path, wait_s, timeout)


__all__ = ["SpeakerBusy", "play_audio_file", "player_command"]
