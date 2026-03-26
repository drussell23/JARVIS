"""
Zero-Copy Shared Memory Ring Buffer Frame Reader (v2)

All reads via ctypes — no Python mmap module (which caches pages).
Handles stale segments from dead writer processes automatically.
"""
from __future__ import annotations

import ctypes
import logging
import os
import struct
import time as _time
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

SHM_NAME = b"/jarvis_frame_bridge"
HEADER_SIZE = 128
RING_SIZE = 5

# libc functions
_libc = ctypes.CDLL("libc.dylib", use_errno=True)
_libc.shm_open.restype = ctypes.c_int
_libc.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint16]
_libc.shm_unlink.restype = ctypes.c_int
_libc.shm_unlink.argtypes = [ctypes.c_char_p]
_libc.mmap.restype = ctypes.c_void_p
_libc.mmap.argtypes = [
    ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_long,
]
_libc.munmap.restype = ctypes.c_int
_libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_libc.close.restype = ctypes.c_int
_libc.close.argtypes = [ctypes.c_int]

_PROT_READ = 0x01
_MAP_SHARED = 0x01
_O_RDONLY = 0x0000
_MAP_FAILED = ctypes.c_void_p(-1).value


class ShmFrameReader:
    """Zero-copy ring buffer reader. All reads via ctypes for live coherency."""

    def __init__(self) -> None:
        self._addr: int = 0       # raw mmap address
        self._size: int = 0       # total mmap size
        self._fd: int = -1
        self._last_counter: int = 0
        self.width: int = 0
        self.height: int = 0
        self.channels: int = 0
        self.frame_size: int = 0

    def open(self) -> bool:
        """Open the shared memory ring buffer."""
        if self._addr:
            return True

        fd = _libc.shm_open(SHM_NAME, _O_RDONLY, 0o666)
        if fd < 0:
            return False

        # Get size
        try:
            total = os.fstat(fd).st_size
        except Exception:
            _libc.close(fd)
            return False

        if total < HEADER_SIZE:
            _libc.close(fd)
            return False

        # Check for stale writer
        # Read writer_pid from a quick mmap, check if alive
        tmp_addr = _libc.mmap(None, HEADER_SIZE, _PROT_READ, _MAP_SHARED, fd, 0)
        if tmp_addr == _MAP_FAILED:
            _libc.close(fd)
            return False

        writer_pid = ctypes.c_uint32.from_address(tmp_addr + 36).value
        _libc.munmap(tmp_addr, HEADER_SIZE)

        try:
            os.kill(writer_pid, 0)  # Check if writer is alive
        except ProcessLookupError:
            # Writer dead — unlink stale segment and retry
            _libc.close(fd)
            _libc.shm_unlink(SHM_NAME)
            logger.info("[ShmRing] Unlinked stale segment (pid %d)", writer_pid)
            _time.sleep(1)  # Wait for fresh writer
            fd = _libc.shm_open(SHM_NAME, _O_RDONLY, 0o666)
            if fd < 0:
                return False
            try:
                total = os.fstat(fd).st_size
            except Exception:
                _libc.close(fd)
                return False

        # Map the full segment
        addr = _libc.mmap(None, total, _PROT_READ, _MAP_SHARED, fd, 0)
        if addr == _MAP_FAILED:
            _libc.close(fd)
            return False

        self._fd = fd
        self._addr = addr
        self._size = total

        # Read dimensions from header
        self.width = ctypes.c_uint32.from_address(addr + 8).value
        self.height = ctypes.c_uint32.from_address(addr + 12).value
        self.channels = ctypes.c_uint32.from_address(addr + 16).value
        self.frame_size = ctypes.c_uint32.from_address(addr + 32).value

        logger.info(
            "[ShmRing] Open: %dx%dx%d frame=%d bytes",
            self.width, self.height, self.channels, self.frame_size,
        )
        return True

    def read_latest(self) -> Tuple[Optional[np.ndarray], int]:
        """Read the latest frame. Zero copy via ctypes + numpy.ctypeslib."""
        if not self._addr:
            return None, 0

        counter = ctypes.c_uint64.from_address(self._addr).value
        if counter == self._last_counter:
            return None, counter

        latest_idx = ctypes.c_uint32.from_address(self._addr + 28).value
        if latest_idx >= RING_SIZE:
            return None, counter

        slot_offset = HEADER_SIZE + (latest_idx * self.frame_size)
        if slot_offset + self.frame_size > self._size:
            return None, counter

        # Zero-copy numpy view over the raw shared memory
        arr_type = ctypes.c_uint8 * self.frame_size
        frame = np.ctypeslib.as_array(
            arr_type.from_address(self._addr + slot_offset)
        ).reshape((self.height, self.width, self.channels))

        self._last_counter = counter
        return frame, counter

    # Legacy alias
    def read_frame(self) -> Tuple[Optional[np.ndarray], int]:
        return self.read_latest()

    def has_new_frame(self) -> bool:
        if not self._addr:
            return False
        counter = ctypes.c_uint64.from_address(self._addr).value
        return counter > self._last_counter

    def close(self) -> None:
        if self._addr:
            _libc.munmap(self._addr, self._size)
            self._addr = 0
        if self._fd >= 0:
            _libc.close(self._fd)
            self._fd = -1

    def __del__(self) -> None:
        self.close()
