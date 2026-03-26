"""
Zero-Copy Shared Memory Frame Reader

Reads raw BGRA frames from POSIX shared memory written by the C++ SCK daemon.
Uses numpy.frombuffer() to create an array view directly over the mapped memory --
zero copy, zero GIL, zero function calls across the language boundary.

The C++ writer (shm_frame_bridge.h) double-buffers: it writes to the inactive
buffer, then atomically flips active_buffer. Python always reads the active
buffer. No locks needed.

Usage::

    reader = ShmFrameReader()
    if reader.open():
        frame, counter = reader.read_frame()
        # frame is a numpy array view over shared memory -- zero copy
        # counter is the monotonic frame number (detect new frames)
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

# Must match shm_frame_bridge.h layout exactly
SHM_NAME = "/jarvis_frame_bridge"
HEADER_SIZE = 64
HEADER_FMT = "<QIIIIQIIxxxxxxxxxxxxxxxxxxxxxxxx"
# Q=frame_counter, I=width, I=height, I=channels, I=active_buffer,
# Q=timestamp_ns, I=writer_pid, I=frame_size, 24 bytes padding


class ShmFrameReader:
    """Zero-copy shared memory frame reader.

    Opens the POSIX shared memory segment created by the C++ SCK daemon
    and returns numpy array views directly over the mapped memory.
    No data is copied. No GIL is acquired for the read.
    """

    def __init__(self) -> None:
        self._mm: Optional[mmap.mmap] = None
        self._fd: int = -1
        self._last_counter: int = 0
        self._width: int = 0
        self._height: int = 0
        self._channels: int = 0
        self._frame_size: int = 0

    def open(self) -> bool:
        """Open the shared memory segment. Returns True on success."""
        if self._mm is not None:
            return True

        try:
            # Open the POSIX shared memory file descriptor
            shm_path = f"/dev/shm{SHM_NAME}"
            # macOS uses /tmp for shm_open files
            if os.path.exists(f"/tmp/com.apple.shm{SHM_NAME}"):
                shm_path = f"/tmp/com.apple.shm{SHM_NAME}"

            # Use ctypes to call shm_open directly
            libc = ctypes.CDLL("libc.dylib", use_errno=True)
            shm_name_bytes = SHM_NAME.encode("utf-8")
            self._fd = libc.shm_open(shm_name_bytes, os.O_RDONLY, 0o666)

            if self._fd < 0:
                errno = ctypes.get_errno()
                logger.debug("[ShmReader] shm_open failed: errno=%d", errno)
                return False

            # Get the size
            stat = os.fstat(self._fd)
            total_size = stat.st_size
            if total_size < HEADER_SIZE:
                os.close(self._fd)
                self._fd = -1
                return False

            # Map it read-only
            self._mm = mmap.mmap(self._fd, total_size, access=mmap.ACCESS_READ)

            # Read header to get frame dimensions
            header_bytes = self._mm[:HEADER_SIZE]
            (
                counter, width, height, channels, active_buf,
                ts_ns, writer_pid, frame_size,
            ) = struct.unpack_from("<QIIIIQII", header_bytes, 0)

            self._width = width
            self._height = height
            self._channels = channels
            self._frame_size = frame_size

            logger.info(
                "[ShmReader] Opened: %dx%dx%d (%d bytes/frame) writer_pid=%d",
                width, height, channels, frame_size, writer_pid,
            )
            return True

        except Exception as exc:
            logger.debug("[ShmReader] open failed: %s", exc)
            self.close()
            return False

    def read_frame(self) -> Tuple[Optional[np.ndarray], int]:
        """Read the latest frame from shared memory. Zero copy.

        Returns (frame, counter) where frame is a numpy array view
        over the shared memory and counter is the monotonic frame number.
        Returns (None, 0) if no new frame or not open.

        The returned array is BGRA format (same as SCK output).
        Green channel is index 1 in both BGRA and RGB.
        """
        if self._mm is None:
            return None, 0

        try:
            # Read header atomically (struct unpack from mmap)
            header_bytes = self._mm[:HEADER_SIZE]
            (
                counter, width, height, channels, active_buf,
                ts_ns, writer_pid, frame_size,
            ) = struct.unpack_from("<QIIIIQII", header_bytes, 0)

            # No new frame?
            if counter == self._last_counter:
                return None, counter

            # Dimensions changed?
            if width != self._width or height != self._height:
                self._width = width
                self._height = height
                self._channels = channels
                self._frame_size = frame_size

            # Calculate buffer offset
            buf_offset = HEADER_SIZE + (active_buf * frame_size)
            buf_end = buf_offset + frame_size

            if buf_end > len(self._mm):
                return None, counter

            # Zero-copy: numpy array VIEW over the mmap buffer
            # This does NOT copy the pixel data. It wraps the memory.
            frame = np.frombuffer(
                self._mm, dtype=np.uint8,
                count=frame_size, offset=buf_offset,
            ).reshape((height, width, channels))

            self._last_counter = counter
            return frame, counter

        except Exception as exc:
            logger.debug("[ShmReader] read failed: %s", exc)
            return None, 0

    def has_new_frame(self) -> bool:
        """Check if a new frame is available without reading it."""
        if self._mm is None:
            return False
        try:
            counter = struct.unpack_from("<Q", self._mm, 0)[0]
            return counter > self._last_counter
        except Exception:
            return False

    @property
    def fps_estimate(self) -> float:
        """Read the current frame counter to estimate activity."""
        if self._mm is None:
            return 0.0
        try:
            counter = struct.unpack_from("<Q", self._mm, 0)[0]
            return float(counter)  # Caller tracks delta/time
        except Exception:
            return 0.0

    def close(self) -> None:
        """Close the shared memory mapping."""
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
