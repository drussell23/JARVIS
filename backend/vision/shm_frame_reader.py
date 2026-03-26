"""
Zero-Copy Shared Memory Ring Buffer Frame Reader

Reads raw BGRA frames from a 5-slot POSIX shared memory ring buffer
written by the C++ SCK daemon. Uses numpy.frombuffer() to create an
array VIEW directly over the mapped memory -- zero copy, zero GIL.

The ring buffer absorbs SCK's bursty delivery. C++ writes at up to
60fps in bursts. Python reads the latest complete frame at any time.
With 5 slots, even if C++ bursts 4 frames while Python processes one,
no frames are torn (the reader always gets a complete frame from the
latest_index slot).

Usage::

    reader = ShmFrameReader()
    if reader.open():
        while True:
            frame, counter = reader.read_latest()
            if frame is not None:
                green = frame[::2, ::2, 1]  # Subsample green channel
                ...
"""
from __future__ import annotations

import ctypes
import logging
import mmap
import os
import struct
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

SHM_NAME = "/jarvis_frame_bridge"
HEADER_SIZE = 128
RING_SIZE = 5

# Header struct: must match RingHeader in shm_frame_bridge.h
# Q=frame_counter, I=width, I=height, I=channels, I=ring_size,
# I=write_index, I=latest_index, I=frame_size, I=writer_pid
# Then 5 Q timestamps, then 48 bytes padding
HEADER_FIXED = "<QIIIIIIII"
HEADER_FIXED_SIZE = struct.calcsize(HEADER_FIXED)  # 40 bytes


class ShmFrameReader:
    """Zero-copy ring buffer reader. Python's eye into the C++ retina."""

    def __init__(self) -> None:
        self._mm: Optional[mmap.mmap] = None
        self._fd: int = -1
        self._last_counter: int = 0
        self._width: int = 0
        self._height: int = 0
        self._channels: int = 0
        self._frame_size: int = 0

    def open(self) -> bool:
        """Open the shared memory ring buffer. Returns True on success."""
        if self._mm is not None:
            return True
        try:
            libc = ctypes.CDLL("libc.dylib", use_errno=True)
            self._fd = libc.shm_open(
                SHM_NAME.encode("utf-8"), os.O_RDONLY, 0o666,
            )
            if self._fd < 0:
                return False

            total_size = os.fstat(self._fd).st_size
            if total_size < HEADER_SIZE:
                os.close(self._fd)
                self._fd = -1
                return False

            self._mm = mmap.mmap(self._fd, total_size, access=mmap.ACCESS_READ)

            # Read static header fields
            fixed = struct.unpack_from(HEADER_FIXED, self._mm, 0)
            (
                _counter, self._width, self._height, self._channels,
                _ring, _wi, _li, self._frame_size, _pid,
            ) = fixed

            logger.info(
                "[ShmRing] Opened: %dx%dx%d ring=%d frame_size=%d",
                self._width, self._height, self._channels, RING_SIZE,
                self._frame_size,
            )
            return True
        except Exception as exc:
            logger.debug("[ShmRing] open failed: %s", exc)
            self.close()
            return False

    def read_latest(self) -> Tuple[Optional[np.ndarray], int]:
        """Read the latest complete frame from the ring buffer. Zero copy.

        Returns (frame_view, counter). frame_view is a numpy array VIEW
        over the mmap -- no data is copied. Returns (None, 0) if no new frame.

        The returned array is BGRA. Green channel is index 1.
        """
        if self._mm is None:
            return None, 0
        try:
            # Read frame_counter (offset 0) and latest_index (offset 28)
            counter = struct.unpack_from("<Q", self._mm, 0)[0]
            if counter == self._last_counter:
                return None, counter  # No new frame

            latest_idx = struct.unpack_from("<I", self._mm, 28)[0]

            # Bounds check
            if latest_idx >= RING_SIZE:
                return None, counter

            # Slot offset in the mmap
            slot_offset = HEADER_SIZE + (latest_idx * self._frame_size)
            slot_end = slot_offset + self._frame_size

            if slot_end > len(self._mm):
                return None, counter

            # Zero-copy: numpy view directly over the mmap buffer
            frame = np.frombuffer(
                self._mm, dtype=np.uint8,
                count=self._frame_size, offset=slot_offset,
            ).reshape((self._height, self._width, self._channels))

            self._last_counter = counter
            return frame, counter

        except Exception:
            return None, 0

    # Legacy alias for compatibility
    def read_frame(self) -> Tuple[Optional[np.ndarray], int]:
        return self.read_latest()

    def has_new_frame(self) -> bool:
        if self._mm is None:
            return False
        try:
            counter = struct.unpack_from("<Q", self._mm, 0)[0]
            return counter > self._last_counter
        except Exception:
            return False

    def close(self) -> None:
        if self._mm is not None:
            try:
                self._mm.close()
            except Exception:
                pass
            self._mm = None
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = -1

    def __del__(self) -> None:
        self.close()
