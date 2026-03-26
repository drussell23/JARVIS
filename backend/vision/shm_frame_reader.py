"""
Zero-Copy Shared Memory Ring Buffer Frame Reader (v3)

SAFE implementation: uses ONLY Python's mmap module (no raw ctypes mmap).
The EXC_GUARD crash was caused by mixing ctypes.mmap with Python's mmap
on the same fd — virtual memory mapping collision.

Fix: Python mmap with ACCESS_WRITE forces coherent page reads (no caching).
We only READ from it, but the write flag prevents the OS from optimizing
away our reads of pages that another process is writing to.
"""
from __future__ import annotations

import ctypes
import logging
import mmap
import os
import struct
import time as _time
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

SHM_NAME = b"/jarvis_frame_bridge"
HEADER_SIZE = 128
RING_SIZE = 5

# libc for shm_open/shm_unlink only (no mmap via ctypes!)
_libc = ctypes.CDLL("libc.dylib", use_errno=True)
_libc.shm_open.restype = ctypes.c_int
_libc.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint16]
_libc.shm_unlink.restype = ctypes.c_int
_libc.shm_unlink.argtypes = [ctypes.c_char_p]


class ShmFrameReader:
    """Safe zero-copy ring buffer reader. Python mmap only — no ctypes mmap."""

    def __init__(self) -> None:
        self._mm: Optional[mmap.mmap] = None
        self._fd: int = -1
        self._last_counter: int = 0
        self.width: int = 0
        self.height: int = 0
        self.channels: int = 0
        self.frame_size: int = 0

    def open(self) -> bool:
        if self._mm is not None:
            return True

        # Open shm fd via libc (Python has no shm_open)
        fd = _libc.shm_open(SHM_NAME, os.O_RDWR, 0o666)
        if fd < 0:
            return False

        try:
            total = os.fstat(fd).st_size
        except Exception:
            os.close(fd)
            return False

        if total < HEADER_SIZE:
            os.close(fd)
            return False

        # Check for stale writer
        try:
            mm_check = mmap.mmap(fd, HEADER_SIZE, access=mmap.ACCESS_READ)
            writer_pid = struct.unpack_from("<I", mm_check, 36)[0]
            mm_check.close()

            try:
                os.kill(writer_pid, 0)
            except ProcessLookupError:
                os.close(fd)
                _libc.shm_unlink(SHM_NAME)
                logger.info("[ShmRing] Stale segment (pid %d dead) — unlinked", writer_pid)
                _time.sleep(1)
                fd = _libc.shm_open(SHM_NAME, os.O_RDWR, 0o666)
                if fd < 0:
                    return False
                total = os.fstat(fd).st_size
        except Exception:
            pass

        # Map with ACCESS_WRITE — forces coherent reads (no page caching).
        # We ONLY read, but the write flag prevents stale cache optimization.
        try:
            self._mm = mmap.mmap(fd, total, access=mmap.ACCESS_WRITE)
        except Exception:
            os.close(fd)
            return False

        self._fd = fd

        # Read header
        self.width = struct.unpack_from("<I", self._mm, 8)[0]
        self.height = struct.unpack_from("<I", self._mm, 12)[0]
        self.channels = struct.unpack_from("<I", self._mm, 16)[0]
        self.frame_size = struct.unpack_from("<I", self._mm, 32)[0]

        logger.info(
            "[ShmRing] Open: %dx%dx%d frame=%d",
            self.width, self.height, self.channels, self.frame_size,
        )
        return True

    def read_latest(self) -> Tuple[Optional[np.ndarray], int]:
        """Read latest frame. Zero copy via numpy.frombuffer over mmap."""
        if self._mm is None:
            return None, 0

        # Read counter — ACCESS_WRITE mmap returns live data
        counter = struct.unpack_from("<Q", self._mm, 0)[0]
        if counter == self._last_counter:
            return None, counter

        latest_idx = struct.unpack_from("<I", self._mm, 28)[0]
        if latest_idx >= RING_SIZE:
            return None, counter

        slot_offset = HEADER_SIZE + (latest_idx * self.frame_size)
        if slot_offset + self.frame_size > len(self._mm):
            return None, counter

        # Zero-copy: numpy view over the mmap buffer
        frame = np.frombuffer(
            self._mm, dtype=np.uint8,
            count=self.frame_size, offset=slot_offset,
        ).reshape((self.height, self.width, self.channels))

        self._last_counter = counter
        return frame, counter

    def read_frame(self) -> Tuple[Optional[np.ndarray], int]:
        return self.read_latest()

    def has_new_frame(self) -> bool:
        if self._mm is None:
            return False
        counter = struct.unpack_from("<Q", self._mm, 0)[0]
        return counter > self._last_counter

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
