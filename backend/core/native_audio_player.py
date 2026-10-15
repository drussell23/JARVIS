"""
native_audio_player - play a synthesized audio file with the OS's own player.

The voice layer plays TTS output OUTSIDE the Python audio stack on purpose:
a PortAudio callback needs the GIL every ~20 ms, so GIL-heavy work turned
speech into static (v283.0). Every platform plays a line the same way -- one
short-lived native player process per line:

* macOS  - ``afplay``.
* Windows - this file, run as a script by the base interpreter in isolated
  mode (``-I -S``: stdlib only, ~40 ms to start). It plays through the MCI
  API in ``winmm`` (``open ... type mpegvideo`` handles MP3 and WAV). Under
  Remote Desktop the session's audio redirection carries it to the client.
* other  - ``mpg123``.

Why a process and not MCI inside the kernel: an audio device belongs to the
process that opened it. When a Remote Desktop client reconnects, Windows
tears the redirected endpoint down and builds a new one; an in-process MCI
kept rendering to the dead one, and every line after the reconnect failed
with MCIERR_INTERNAL (277) while a fresh process played fine (the 08:55 boot
on 2026-10-15: last line spoken at 08:56:13, session reconnect at 08:56:14,
silent from then on). A player process meets the endpoint as it is NOW.
It is also the only thing a timeout can stop: ``play ... wait`` in a worker
thread could be neither bounded nor cancelled.

There is ONE speaker. Every caller -- the voice coordinator's parallel
workers, the orchestrator's fallback, narration -- plays through
:func:`play_audio_file`, and it plays one file at a time: synthesis may run in
parallel, playback never does. The coordinator ran several workers whose
playbacks overlapped, and the operator heard JARVIS talking over himself.
The lock is a thread lock held by the thread that plays, so it holds across
event loops and across the asyncio cancellation of a caller (a cancelled
caller's line still finishes, then releases the speaker).

A line that did not play raises: the player's exit status is the verdict,
and its own words (MCI's error text, afplay's stderr) are the reason.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import threading
from typing import List, Optional

_speaker = threading.Lock()

#: How much of a failed player's stderr travels in the error it raises.
_PLAYER_ERROR_CHARS = 400


class SpeakerBusy(TimeoutError):
    """The speaker stayed busy past the caller's wait: the line is stale."""


# --- the Windows player (runs in its own process) ----------------------------

def _mci(command: str) -> int:
    import ctypes

    buf = ctypes.create_unicode_buffer(256)
    return ctypes.windll.winmm.mciSendStringW(command, buf, len(buf), None)


def _mci_error(code: int) -> str:
    import ctypes

    buf = ctypes.create_unicode_buffer(256)
    if ctypes.windll.winmm.mciGetErrorStringW(code, buf, len(buf)) and buf.value:
        return f"MCI error {code}: {buf.value}"
    return f"MCI error {code}"


def _mci_play_blocking(path: str) -> None:
    """Play *path* to its end through MCI. Raises OSError with MCI's own
    words. One alias per process: this runs as the player process."""
    alias = f"jarvis_tts_{os.getpid()}"
    err = _mci(f'open "{path}" type mpegvideo alias {alias}')
    if err:
        raise OSError(f"open {path}: {_mci_error(err)}")
    try:
        err = _mci(f"play {alias} wait")
        if err:
            raise OSError(f"play {path}: {_mci_error(err)}")
    finally:
        _mci(f"close {alias}")


def _interpreter() -> str:
    """The interpreter that runs the Windows player. A venv's python.exe is
    a launcher that starts the base interpreter as a CHILD; stopping the
    launcher on a timeout would leave the child playing. The player needs
    only the stdlib, so it runs on the base interpreter directly."""
    return getattr(sys, "_base_executable", None) or sys.executable


# --- the speaker -------------------------------------------------------------

def player_command(path: str) -> List[str]:
    """The native player argv that plays ``path`` to completion."""
    if sys.platform == "win32":
        return [_interpreter(), "-I", "-S", os.path.abspath(__file__), path]
    if sys.platform == "darwin" or shutil.which("afplay"):
        return ["afplay", path]
    return ["mpg123", "-q", path]


def _run_player(argv: List[str], timeout: Optional[float]) -> None:
    """Run one player process; raise unless it played the line."""
    try:
        done = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=timeout, check=False,
            # The player has nothing to show: no console window of its own
            # when the kernel runs without one.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:  # run() has already stopped it
        raise TimeoutError(f"playback still running after {exc.timeout:.1f}s; stopped") from None
    if done.returncode != 0:
        said = (done.stderr or b"").decode("utf-8", "replace").strip()
        raise OSError(
            f"{os.path.basename(argv[0])} exited {done.returncode}"
            + (f": {said[-_PLAYER_ERROR_CHARS:]}" if said else "")
        )


def _play_exclusive(path: str, wait_s: Optional[float], timeout: Optional[float]) -> None:
    """Take the speaker (waiting at most *wait_s*; None = until free), play
    *path* to completion, release. Runs in a worker thread."""
    if not _speaker.acquire(timeout=-1 if wait_s is None else max(0.0, wait_s)):
        raise SpeakerBusy(f"speaker busy for {wait_s:.1f}s")
    try:
        _run_player(player_command(path), timeout)
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


if __name__ == "__main__":
    # The Windows player process (see player_command). Exit status is the
    # verdict; stderr carries MCI's reason to the kernel.
    if len(sys.argv) != 2:
        sys.stderr.write("usage: native_audio_player.py <audio file>\n")
        sys.exit(2)
    try:
        _mci_play_blocking(sys.argv[1])
    except OSError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.exit(1)
