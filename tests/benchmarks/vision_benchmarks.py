#!/usr/bin/env python3
"""
JARVIS Vision Pipeline Benchmark Suite

Comprehensive performance benchmarks for every stage of the vision pipeline:
  1. SHM Read Speed         — synthetic ring buffer polling throughput
  2. SCK -> SHM Delivery    — full pipeline: SCK capture -> SHM write -> Python read
  3. BGRA->RGB Conversion   — channel conversion overhead at multiple resolutions
  4. Motion Detection (dhash) — per-frame dhash cost
  5. End-to-End FramePipeline — full pipeline with motion detection + queue management

Each benchmark reports: fps, latency (mean/median/p95/p99/max), throughput (MB/s).
Results are written as JSON for downstream analysis (Jupyter notebooks, CI dashboards).

Usage:
    python3 tests/benchmarks/vision_benchmarks.py
    python3 tests/benchmarks/vision_benchmarks.py --duration 10 --save-json results.json
    python3 tests/benchmarks/vision_benchmarks.py --skip-sck --only bgra dhash
    python3 tests/benchmarks/vision_benchmarks.py --resolutions 1920x1080 2560x1440 3840x2160
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import mmap
import os
import signal
import struct
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_NATIVE_EXT = _PROJECT_ROOT / "backend" / "native_extensions"

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(_NATIVE_EXT) not in sys.path:
    sys.path.insert(0, str(_NATIVE_EXT))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_WIDTH = 1440
DEFAULT_HEIGHT = 900
DEFAULT_CHANNELS = 4  # BGRA
HEADER_SIZE = 128
RING_SIZE = 5
DEFAULT_DURATION_S = 5.0
DEFAULT_OUTPUT_DIR = "/tmp/claude/vision_benchmarks"

# SHM header layout (matches shm_frame_reader.py / SCK writer):
# offset  0: uint64  counter
# offset  8: uint32  width
# offset 12: uint32  height
# offset 16: uint32  channels
# offset 20: uint32  reserved
# offset 24: uint32  reserved
# offset 28: uint32  latest_idx
# offset 32: uint32  frame_size
# offset 36: uint32  writer_pid

logger = logging.getLogger("vision_benchmarks")

# ---------------------------------------------------------------------------
# Data classes for structured results
# ---------------------------------------------------------------------------

@dataclass
class LatencyStats:
    """Latency statistics in microseconds."""
    mean_us: float = 0.0
    median_us: float = 0.0
    p95_us: float = 0.0
    p99_us: float = 0.0
    max_us: float = 0.0
    min_us: float = 0.0
    stddev_us: float = 0.0

    @classmethod
    def from_samples(cls, latencies_s: Sequence[float]) -> "LatencyStats":
        """Compute stats from a list of latency values in seconds."""
        if not latencies_s:
            return cls()
        arr = np.array(latencies_s) * 1_000_000  # seconds -> microseconds
        return cls(
            mean_us=float(np.mean(arr)),
            median_us=float(np.median(arr)),
            p95_us=float(np.percentile(arr, 95)),
            p99_us=float(np.percentile(arr, 99)),
            max_us=float(np.max(arr)),
            min_us=float(np.min(arr)),
            stddev_us=float(np.std(arr)),
        )


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run."""
    name: str
    description: str
    duration_s: float = 0.0
    total_operations: int = 0
    fps: float = 0.0
    throughput_mb_s: float = 0.0
    latency: LatencyStats = field(default_factory=LatencyStats)
    parameters: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class BenchmarkSuite:
    """Container for all benchmark results."""
    timestamp: str = ""
    platform: str = ""
    python_version: str = ""
    numpy_version: str = ""
    results: List[BenchmarkResult] = field(default_factory=list)
    config: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_resolution(s: str) -> Tuple[int, int]:
    """Parse 'WIDTHxHEIGHT' string into (width, height) tuple."""
    parts = s.lower().split("x")
    if len(parts) != 2:
        raise ValueError(f"Invalid resolution format: {s!r} (expected WxH)")
    return int(parts[0]), int(parts[1])


def _generate_synthetic_frame(
    width: int, height: int, channels: int, frame_number: int
) -> np.ndarray:
    """Generate a synthetic BGRA frame with deterministic but varied content.

    Uses a combination of gradient and frame-number-seeded noise to produce
    frames that are visually distinct (important for dhash/motion benchmarks).
    """
    rng = np.random.default_rng(seed=frame_number)
    # Base gradient
    row = np.linspace(0, 255, width, dtype=np.uint8)
    col = np.linspace(0, 255, height, dtype=np.uint8)
    gradient = np.outer(col, row).astype(np.uint8)
    # Build multi-channel frame
    frame = np.zeros((height, width, channels), dtype=np.uint8)
    frame[:, :, 0] = gradient  # B
    frame[:, :, 1] = np.roll(gradient, frame_number * 7, axis=1)  # G
    frame[:, :, 2] = np.roll(gradient, frame_number * 13, axis=0)  # R
    if channels == 4:
        frame[:, :, 3] = 255  # Alpha
    # Add localized noise patch (simulates real content variation)
    patch_y = rng.integers(0, max(1, height - 64))
    patch_x = rng.integers(0, max(1, width - 64))
    noise = rng.integers(0, 256, size=(min(64, height), min(64, width), channels), dtype=np.uint8)
    frame[patch_y:patch_y + noise.shape[0], patch_x:patch_x + noise.shape[1], :] = noise
    return frame


def _frame_size_bytes(width: int, height: int, channels: int) -> int:
    return width * height * channels


@contextmanager
def _synthetic_shm(
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    channels: int = DEFAULT_CHANNELS,
):
    """Create a temporary POSIX shared memory segment mimicking the SCK writer layout.

    Yields (shm_name, fd, mm, frame_size) and cleans up on exit.
    """
    frame_size = _frame_size_bytes(width, height, channels)
    total_size = HEADER_SIZE + (RING_SIZE * frame_size)

    # Use a unique name to avoid collisions with the real bridge
    shm_name = f"/jarvis_bench_{os.getpid()}".encode()

    libc = ctypes.CDLL("libc.dylib", use_errno=True)
    libc.shm_open.restype = ctypes.c_int
    libc.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint16]
    libc.shm_unlink.restype = ctypes.c_int
    libc.shm_unlink.argtypes = [ctypes.c_char_p]

    # O_CREAT | O_RDWR | O_EXCL = ensure fresh segment
    O_CREAT = 0x0200
    O_RDWR = 0x0002
    O_EXCL = 0x0800

    # Remove any stale segment first
    libc.shm_unlink(shm_name)

    fd = libc.shm_open(shm_name, O_CREAT | O_RDWR | O_EXCL, 0o666)
    if fd < 0:
        errno = ctypes.get_errno()
        raise OSError(f"shm_open failed: errno={errno}")

    try:
        os.ftruncate(fd, total_size)
        mm = mmap.mmap(fd, total_size, access=mmap.ACCESS_WRITE)

        # Write header
        struct.pack_into("<Q", mm, 0, 0)        # counter = 0
        struct.pack_into("<I", mm, 8, width)     # width
        struct.pack_into("<I", mm, 12, height)   # height
        struct.pack_into("<I", mm, 16, channels) # channels
        struct.pack_into("<I", mm, 28, 0)        # latest_idx
        struct.pack_into("<I", mm, 32, frame_size)  # frame_size
        struct.pack_into("<I", mm, 36, os.getpid())  # writer_pid

        yield shm_name, fd, mm, frame_size

    finally:
        try:
            mm.close()
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
        libc.shm_unlink(shm_name)


def _write_frame_to_shm(
    mm: mmap.mmap,
    frame: np.ndarray,
    counter: int,
    frame_size: int,
) -> int:
    """Write a frame into the SHM ring buffer and update header.

    Returns the new counter value.
    """
    slot_idx = counter % RING_SIZE
    offset = HEADER_SIZE + (slot_idx * frame_size)
    mm[offset:offset + frame_size] = frame.tobytes()

    new_counter = counter + 1
    struct.pack_into("<Q", mm, 0, new_counter)   # counter
    struct.pack_into("<I", mm, 28, slot_idx)     # latest_idx
    return new_counter


# ---------------------------------------------------------------------------
# Benchmark 1: SHM Read Throughput (Synthetic)
# ---------------------------------------------------------------------------

def bench_shm_read_speed(
    duration_s: float = DEFAULT_DURATION_S,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    channels: int = DEFAULT_CHANNELS,
) -> BenchmarkResult:
    """Benchmark pure SHM ring buffer read speed using synthetic data.

    Creates a temporary SHM segment, pre-populates it with frames, and
    measures how fast ShmFrameReader.read_latest() can poll new frames.

    This isolates the SHM read path without any SCK overhead.
    """
    result = BenchmarkResult(
        name="shm_read_speed",
        description="SHM ring buffer read throughput (synthetic writer, no SCK)",
        parameters={
            "width": width, "height": height, "channels": channels,
            "duration_s": duration_s, "ring_size": RING_SIZE,
        },
    )

    frame_size = _frame_size_bytes(width, height, channels)

    try:
        with _synthetic_shm(width, height, channels) as (shm_name, fd, mm, fsize):
            # Pre-generate a bank of synthetic frames
            num_pregenerated = RING_SIZE * 2
            frames = [
                _generate_synthetic_frame(width, height, channels, i)
                for i in range(num_pregenerated)
            ]

            # Measure read speed: we write frames into SHM and immediately
            # read them back, measuring the read path latency.
            latencies: List[float] = []
            counter = 0
            ops = 0
            start = time.perf_counter()
            deadline = start + duration_s

            while time.perf_counter() < deadline:
                # Write a new frame into the ring buffer
                frame = frames[ops % num_pregenerated]
                counter = _write_frame_to_shm(mm, frame, counter, fsize)

                # Read it back (simulate ShmFrameReader.read_latest logic)
                t0 = time.perf_counter()

                read_counter = struct.unpack_from("<Q", mm, 0)[0]
                latest_idx = struct.unpack_from("<I", mm, 28)[0]
                slot_offset = HEADER_SIZE + (latest_idx * fsize)
                frame_arr = np.frombuffer(
                    mm, dtype=np.uint8, count=fsize, offset=slot_offset,
                ).reshape((height, width, channels))

                t1 = time.perf_counter()
                latencies.append(t1 - t0)
                ops += 1

            elapsed = time.perf_counter() - start
            result.duration_s = elapsed
            result.total_operations = ops
            result.fps = ops / elapsed if elapsed > 0 else 0
            result.throughput_mb_s = (ops * frame_size) / (elapsed * 1024 * 1024) if elapsed > 0 else 0
            result.latency = LatencyStats.from_samples(latencies)

    except Exception as exc:
        result.error = str(exc)

    return result


# ---------------------------------------------------------------------------
# Benchmark 2: SCK -> SHM Delivery Rate (requires screen recording)
# ---------------------------------------------------------------------------

def bench_sck_to_shm(
    duration_s: float = DEFAULT_DURATION_S,
    target_fps: int = 60,
    window_id: int = 0,
) -> BenchmarkResult:
    """Benchmark full SCK -> SHM -> Python read pipeline.

    Requires screen recording permission. Starts SCK in a background thread,
    opens the real SHM ring buffer, and measures actual frame delivery rate.
    """
    result = BenchmarkResult(
        name="sck_to_shm_delivery",
        description="Full SCK capture -> SHM write -> Python read pipeline",
        parameters={
            "target_fps": target_fps, "duration_s": duration_s,
            "window_id": window_id,
        },
    )

    import threading

    try:
        import fast_capture_stream
    except ImportError as exc:
        result.error = f"fast_capture_stream not available: {exc}"
        return result

    # Check screen recording permission
    try:
        import Quartz
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
        )
        if not windows or len(windows) == 0:
            result.error = "Screen recording permission not granted"
            return result
    except ImportError:
        result.error = "Quartz framework not available"
        return result

    ready_event = threading.Event()
    stop_event = threading.Event()
    thread_error: List[Optional[str]] = [None]

    def _sck_thread():
        try:
            config = fast_capture_stream.StreamConfig()
            config.target_fps = target_fps
            config.max_buffer_size = 3
            config.output_format = "raw"
            config.use_gpu_acceleration = True
            config.drop_frames_on_overflow = True

            stream = fast_capture_stream.CaptureStream(window_id, config)
            if not stream.start():
                thread_error[0] = "stream.start() returned False"
                return

            ready_event.set()
            while not stop_event.is_set():
                stop_event.wait(timeout=0.1)
            stream.stop()
        except Exception as exc:
            thread_error[0] = str(exc)

    t = threading.Thread(target=_sck_thread, daemon=True, name="bench-sck")
    t.start()

    if not ready_event.wait(timeout=10.0):
        stop_event.set()
        result.error = thread_error[0] or "SCK thread did not start within 10s"
        return result

    if thread_error[0]:
        result.error = thread_error[0]
        return result

    # Let SCK warm up and write initial frames
    time.sleep(1.0)

    try:
        from backend.vision.shm_frame_reader import ShmFrameReader

        reader = ShmFrameReader()
        if not reader.open():
            result.error = "ShmFrameReader.open() returned False"
            stop_event.set()
            return result

        frame_size = reader.frame_size
        width, height, channels = reader.width, reader.height, reader.channels

        result.parameters.update({
            "width": width, "height": height, "channels": channels,
            "frame_size": frame_size,
        })

        latencies: List[float] = []
        inter_frame_times: List[float] = []
        ops = 0
        empty_polls = 0
        last_frame_ts = time.perf_counter()
        start = time.perf_counter()
        deadline = start + duration_s

        while time.perf_counter() < deadline:
            t0 = time.perf_counter()
            frame_arr, counter = reader.read_latest()
            t1 = time.perf_counter()

            if frame_arr is not None:
                latencies.append(t1 - t0)
                now = time.perf_counter()
                if ops > 0:
                    inter_frame_times.append(now - last_frame_ts)
                last_frame_ts = now
                ops += 1
            else:
                empty_polls += 1
                # Brief yield to avoid pure busy-spin
                time.sleep(0.0001)

        elapsed = time.perf_counter() - start

        result.duration_s = elapsed
        result.total_operations = ops
        result.fps = ops / elapsed if elapsed > 0 else 0
        result.throughput_mb_s = (ops * frame_size) / (elapsed * 1024 * 1024) if elapsed > 0 else 0
        result.latency = LatencyStats.from_samples(latencies)
        result.metadata["empty_polls"] = empty_polls
        result.metadata["poll_efficiency_pct"] = (
            round(100.0 * ops / (ops + empty_polls), 2) if (ops + empty_polls) > 0 else 0
        )
        if inter_frame_times:
            ift = np.array(inter_frame_times) * 1_000_000
            result.metadata["inter_frame_us"] = {
                "mean": float(np.mean(ift)),
                "median": float(np.median(ift)),
                "p95": float(np.percentile(ift, 95)),
                "max": float(np.max(ift)),
            }

        reader.close()

    except Exception as exc:
        result.error = str(exc)
    finally:
        stop_event.set()
        t.join(timeout=5.0)

    return result


# ---------------------------------------------------------------------------
# Benchmark 3: BGRA -> RGB Conversion
# ---------------------------------------------------------------------------

def bench_bgra_to_rgb(
    duration_s: float = DEFAULT_DURATION_S,
    resolutions: Optional[List[Tuple[int, int]]] = None,
) -> List[BenchmarkResult]:
    """Benchmark BGRA -> RGB channel conversion at various resolutions.

    Tests three conversion strategies:
      - numpy fancy indexing (frame[:, :, [2, 1, 0]]) -- what the pipeline uses
      - numpy slicing with flip (manual slice + stack)
      - PIL Image conversion
    """
    if resolutions is None:
        resolutions = [(1440, 900), (1920, 1080), (2560, 1440), (3840, 2160)]

    results: List[BenchmarkResult] = []

    for width, height in resolutions:
        channels = 4  # BGRA
        frame_size = _frame_size_bytes(width, height, channels)
        # Pre-generate a frame
        frame_bgra = _generate_synthetic_frame(width, height, channels, 42)

        # --- Strategy 1: numpy fancy indexing (production path) ---
        result_fancy = BenchmarkResult(
            name=f"bgra_to_rgb_fancy_{width}x{height}",
            description=f"BGRA->RGB via numpy fancy indexing at {width}x{height}",
            parameters={"width": width, "height": height, "method": "fancy_index"},
        )
        latencies: List[float] = []
        ops = 0
        start = time.perf_counter()
        deadline = start + duration_s

        while time.perf_counter() < deadline:
            t0 = time.perf_counter()
            rgb = frame_bgra[:, :, [2, 1, 0]]
            t1 = time.perf_counter()
            latencies.append(t1 - t0)
            ops += 1
            # Prevent the compiler from optimizing away the result
            if rgb.shape[0] == -1:
                break

        elapsed = time.perf_counter() - start
        result_fancy.duration_s = elapsed
        result_fancy.total_operations = ops
        result_fancy.fps = ops / elapsed if elapsed > 0 else 0
        rgb_size = width * height * 3  # output size
        result_fancy.throughput_mb_s = (ops * rgb_size) / (elapsed * 1024 * 1024) if elapsed > 0 else 0
        result_fancy.latency = LatencyStats.from_samples(latencies)
        results.append(result_fancy)

        # --- Strategy 2: numpy slice + concatenate ---
        result_slice = BenchmarkResult(
            name=f"bgra_to_rgb_slice_{width}x{height}",
            description=f"BGRA->RGB via numpy slice+stack at {width}x{height}",
            parameters={"width": width, "height": height, "method": "slice_stack"},
        )
        latencies = []
        ops = 0
        start = time.perf_counter()
        deadline = start + duration_s

        while time.perf_counter() < deadline:
            t0 = time.perf_counter()
            r = frame_bgra[:, :, 2]
            g = frame_bgra[:, :, 1]
            b = frame_bgra[:, :, 0]
            rgb = np.stack([r, g, b], axis=2)
            t1 = time.perf_counter()
            latencies.append(t1 - t0)
            ops += 1

        elapsed = time.perf_counter() - start
        result_slice.duration_s = elapsed
        result_slice.total_operations = ops
        result_slice.fps = ops / elapsed if elapsed > 0 else 0
        result_slice.throughput_mb_s = (ops * rgb_size) / (elapsed * 1024 * 1024) if elapsed > 0 else 0
        result_slice.latency = LatencyStats.from_samples(latencies)
        results.append(result_slice)

        # --- Strategy 3: contiguous copy (ascontiguousarray after fancy) ---
        result_contig = BenchmarkResult(
            name=f"bgra_to_rgb_contig_{width}x{height}",
            description=f"BGRA->RGB via fancy index + ascontiguousarray at {width}x{height}",
            parameters={"width": width, "height": height, "method": "contig_copy"},
        )
        latencies = []
        ops = 0
        start = time.perf_counter()
        deadline = start + duration_s

        while time.perf_counter() < deadline:
            t0 = time.perf_counter()
            rgb = np.ascontiguousarray(frame_bgra[:, :, [2, 1, 0]])
            t1 = time.perf_counter()
            latencies.append(t1 - t0)
            ops += 1

        elapsed = time.perf_counter() - start
        result_contig.duration_s = elapsed
        result_contig.total_operations = ops
        result_contig.fps = ops / elapsed if elapsed > 0 else 0
        result_contig.throughput_mb_s = (ops * rgb_size) / (elapsed * 1024 * 1024) if elapsed > 0 else 0
        result_contig.latency = LatencyStats.from_samples(latencies)
        results.append(result_contig)

    return results


# ---------------------------------------------------------------------------
# Benchmark 4: dhash Motion Detection
# ---------------------------------------------------------------------------

def bench_dhash_motion(
    duration_s: float = DEFAULT_DURATION_S,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    hash_size: int = 8,
) -> BenchmarkResult:
    """Benchmark dhash computation cost per frame.

    Measures the full dhash pipeline: grayscale conversion, PIL resize,
    gradient computation, and bit packing.
    """
    result = BenchmarkResult(
        name="dhash_motion_detection",
        description=f"dhash computation per frame ({width}x{height}, hash_size={hash_size})",
        parameters={
            "width": width, "height": height, "hash_size": hash_size,
            "duration_s": duration_s,
        },
    )

    try:
        from backend.vision.realtime.frame_pipeline import _dhash, MotionDetector
    except ImportError as exc:
        result.error = f"Failed to import frame_pipeline: {exc}"
        return result

    # Pre-generate varied frames (each visually distinct for realistic dhash load)
    num_frames = 200
    frames = [
        _generate_synthetic_frame(width, height, 3, i)  # RGB for dhash
        for i in range(num_frames)
    ]

    # --- Benchmark raw _dhash function ---
    latencies: List[float] = []
    ops = 0
    start = time.perf_counter()
    deadline = start + duration_s

    while time.perf_counter() < deadline:
        frame = frames[ops % num_frames]
        t0 = time.perf_counter()
        h = _dhash(frame, hash_size)
        t1 = time.perf_counter()
        latencies.append(t1 - t0)
        ops += 1

    elapsed = time.perf_counter() - start
    frame_size_rgb = width * height * 3
    result.duration_s = elapsed
    result.total_operations = ops
    result.fps = ops / elapsed if elapsed > 0 else 0
    result.throughput_mb_s = (ops * frame_size_rgb) / (elapsed * 1024 * 1024) if elapsed > 0 else 0
    result.latency = LatencyStats.from_samples(latencies)

    # Also benchmark full MotionDetector.detect_change (includes hamming + debounce)
    detector = MotionDetector(threshold=0.05, debounce_ms=0, hash_size=hash_size)
    detect_latencies: List[float] = []
    detect_ops = 0
    detect_changes = 0
    start2 = time.perf_counter()
    deadline2 = start2 + duration_s

    while time.perf_counter() < deadline2:
        frame = frames[detect_ops % num_frames]
        t0 = time.perf_counter()
        changed = detector.detect_change(frame)
        t1 = time.perf_counter()
        detect_latencies.append(t1 - t0)
        detect_ops += 1
        if changed:
            detect_changes += 1

    detect_elapsed = time.perf_counter() - start2
    result.metadata["detect_change"] = {
        "ops": detect_ops,
        "fps": detect_ops / detect_elapsed if detect_elapsed > 0 else 0,
        "changes_detected": detect_changes,
        "change_rate_pct": round(100.0 * detect_changes / detect_ops, 2) if detect_ops > 0 else 0,
        "latency": asdict(LatencyStats.from_samples(detect_latencies)),
    }

    return result


# ---------------------------------------------------------------------------
# Benchmark 5: End-to-End FramePipeline
# ---------------------------------------------------------------------------

async def bench_frame_pipeline(
    duration_s: float = DEFAULT_DURATION_S,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    max_queue_size: int = 10,
    motion_detect: bool = True,
    motion_threshold: float = 0.05,
) -> BenchmarkResult:
    """Benchmark full FramePipeline: enqueue + motion detection + queue management.

    Uses FramePipeline in mock mode (use_sck=False) and feeds synthetic frames
    through _enqueue_frame, measuring the full path including motion detection
    and bounded queue overflow handling.
    """
    result = BenchmarkResult(
        name="frame_pipeline_e2e",
        description=(
            f"End-to-end FramePipeline ({width}x{height}, "
            f"queue={max_queue_size}, motion={'on' if motion_detect else 'off'})"
        ),
        parameters={
            "width": width, "height": height, "max_queue_size": max_queue_size,
            "motion_detect": motion_detect, "motion_threshold": motion_threshold,
            "duration_s": duration_s,
        },
    )

    try:
        from backend.vision.realtime.frame_pipeline import FrameData, FramePipeline
    except ImportError as exc:
        result.error = f"Failed to import FramePipeline: {exc}"
        return result

    pipeline = FramePipeline(
        use_sck=False,
        max_queue_size=max_queue_size,
        motion_detect=motion_detect,
        motion_threshold=motion_threshold,
        motion_debounce_ms=0,  # No debounce for benchmark
    )

    await pipeline.start()

    # Pre-generate frames
    num_pregenerated = 200
    frames_rgb = [
        _generate_synthetic_frame(width, height, 3, i)
        for i in range(num_pregenerated)
    ]

    enqueue_latencies: List[float] = []
    consume_latencies: List[float] = []
    enqueued = 0
    consumed = 0
    dropped = 0
    filtered_by_motion = 0
    ops = 0

    frame_size_rgb = width * height * 3
    start = time.perf_counter()
    deadline = start + duration_s

    while time.perf_counter() < deadline:
        rgb = frames_rgb[ops % num_pregenerated]
        ops += 1

        frame_data = FrameData(
            data=rgb,
            width=width,
            height=height,
            timestamp=time.time(),
            frame_number=ops,
            scale_factor=1.0,
        )

        # Measure enqueue path (motion check + queue push)
        t0 = time.perf_counter()
        should = pipeline._should_process(frame_data)
        if should:
            was_full = pipeline._frame_queue.full()
            pipeline._enqueue_frame(frame_data)
            enqueued += 1
            if was_full:
                dropped += 1
        else:
            filtered_by_motion += 1
        t1 = time.perf_counter()
        enqueue_latencies.append(t1 - t0)

        # Periodically consume from queue to prevent permanent saturation
        if enqueued % 5 == 0 and not pipeline._frame_queue.empty():
            tc0 = time.perf_counter()
            try:
                f = pipeline._frame_queue.get_nowait()
                consumed += 1
            except asyncio.QueueEmpty:
                pass
            tc1 = time.perf_counter()
            consume_latencies.append(tc1 - tc0)

    elapsed = time.perf_counter() - start

    await pipeline.stop()

    result.duration_s = elapsed
    result.total_operations = ops
    result.fps = ops / elapsed if elapsed > 0 else 0
    result.throughput_mb_s = (ops * frame_size_rgb) / (elapsed * 1024 * 1024) if elapsed > 0 else 0
    result.latency = LatencyStats.from_samples(enqueue_latencies)
    result.metadata["enqueued"] = enqueued
    result.metadata["consumed"] = consumed
    result.metadata["dropped_overflow"] = dropped
    result.metadata["filtered_by_motion"] = filtered_by_motion
    result.metadata["filter_rate_pct"] = (
        round(100.0 * filtered_by_motion / ops, 2) if ops > 0 else 0
    )
    if consume_latencies:
        result.metadata["consume_latency"] = asdict(
            LatencyStats.from_samples(consume_latencies)
        )

    return result


# ---------------------------------------------------------------------------
# Additional micro-benchmarks
# ---------------------------------------------------------------------------

def bench_shm_write_speed(
    duration_s: float = DEFAULT_DURATION_S,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    channels: int = DEFAULT_CHANNELS,
) -> BenchmarkResult:
    """Benchmark SHM write throughput (simulates SCK delegate write path)."""
    result = BenchmarkResult(
        name="shm_write_speed",
        description=f"SHM ring buffer write throughput ({width}x{height}x{channels})",
        parameters={"width": width, "height": height, "channels": channels},
    )

    frame_size = _frame_size_bytes(width, height, channels)

    try:
        with _synthetic_shm(width, height, channels) as (shm_name, fd, mm, fsize):
            frames = [
                _generate_synthetic_frame(width, height, channels, i)
                for i in range(10)
            ]

            latencies: List[float] = []
            counter = 0
            ops = 0
            start = time.perf_counter()
            deadline = start + duration_s

            while time.perf_counter() < deadline:
                frame = frames[ops % len(frames)]
                t0 = time.perf_counter()
                counter = _write_frame_to_shm(mm, frame, counter, fsize)
                t1 = time.perf_counter()
                latencies.append(t1 - t0)
                ops += 1

            elapsed = time.perf_counter() - start
            result.duration_s = elapsed
            result.total_operations = ops
            result.fps = ops / elapsed if elapsed > 0 else 0
            result.throughput_mb_s = (ops * frame_size) / (elapsed * 1024 * 1024) if elapsed > 0 else 0
            result.latency = LatencyStats.from_samples(latencies)

    except Exception as exc:
        result.error = str(exc)

    return result


def bench_numpy_frombuffer(
    duration_s: float = DEFAULT_DURATION_S,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    channels: int = DEFAULT_CHANNELS,
) -> BenchmarkResult:
    """Benchmark numpy.frombuffer + reshape (the zero-copy view creation)."""
    result = BenchmarkResult(
        name="numpy_frombuffer_reshape",
        description=f"np.frombuffer + reshape ({width}x{height}x{channels})",
        parameters={"width": width, "height": height, "channels": channels},
    )

    frame_size = _frame_size_bytes(width, height, channels)
    # Use a raw bytes buffer to simulate the mmap view
    raw = np.random.randint(0, 256, size=frame_size, dtype=np.uint8).tobytes()

    latencies: List[float] = []
    ops = 0
    start = time.perf_counter()
    deadline = start + duration_s

    while time.perf_counter() < deadline:
        t0 = time.perf_counter()
        arr = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, channels))
        t1 = time.perf_counter()
        latencies.append(t1 - t0)
        ops += 1

    elapsed = time.perf_counter() - start
    result.duration_s = elapsed
    result.total_operations = ops
    result.fps = ops / elapsed if elapsed > 0 else 0
    result.throughput_mb_s = (ops * frame_size) / (elapsed * 1024 * 1024) if elapsed > 0 else 0
    result.latency = LatencyStats.from_samples(latencies)
    return result


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_ALL_BENCHMARKS = [
    "shm_read", "shm_write", "sck_to_shm", "bgra",
    "dhash", "pipeline", "frombuffer",
]


def _print_result(r: BenchmarkResult, verbose: bool = False) -> None:
    """Pretty-print a single benchmark result."""
    status = "OK" if r.error is None else f"FAIL: {r.error}"
    print(f"\n{'=' * 72}")
    print(f"  {r.name}  [{status}]")
    print(f"  {r.description}")
    print(f"{'=' * 72}")

    if r.error:
        return

    print(f"  Duration:    {r.duration_s:.3f}s")
    print(f"  Operations:  {r.total_operations:,}")
    print(f"  FPS:         {r.fps:,.1f}")
    print(f"  Throughput:  {r.throughput_mb_s:,.1f} MB/s")
    print(f"  Latency:")
    lat = r.latency
    print(f"    mean:   {lat.mean_us:>10.1f} us")
    print(f"    median: {lat.median_us:>10.1f} us")
    print(f"    p95:    {lat.p95_us:>10.1f} us")
    print(f"    p99:    {lat.p99_us:>10.1f} us")
    print(f"    max:    {lat.max_us:>10.1f} us")
    print(f"    min:    {lat.min_us:>10.1f} us")
    print(f"    stddev: {lat.stddev_us:>10.1f} us")

    if verbose and r.metadata:
        print(f"  Metadata:")
        for k, v in r.metadata.items():
            if isinstance(v, dict):
                print(f"    {k}:")
                for k2, v2 in v.items():
                    if isinstance(v2, float):
                        print(f"      {k2}: {v2:.1f}")
                    else:
                        print(f"      {k2}: {v2}")
            else:
                print(f"    {k}: {v}")


async def run_benchmarks(
    duration_s: float = DEFAULT_DURATION_S,
    skip_sck: bool = False,
    only: Optional[List[str]] = None,
    resolutions: Optional[List[Tuple[int, int]]] = None,
    verbose: bool = False,
    save_json: Optional[str] = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> BenchmarkSuite:
    """Run the full benchmark suite."""

    active = set(only) if only else set(_ALL_BENCHMARKS)

    suite = BenchmarkSuite(
        timestamp=datetime.now(timezone.utc).isoformat(),
        platform=f"{sys.platform} ({os.uname().machine})",
        python_version=sys.version.split()[0],
        numpy_version=np.__version__,
        config={
            "duration_s": duration_s,
            "skip_sck": skip_sck,
            "width": width,
            "height": height,
            "benchmarks_requested": sorted(active),
        },
    )

    print(f"\nJARVIS Vision Pipeline Benchmark Suite")
    print(f"{'=' * 72}")
    print(f"  Platform:    {suite.platform}")
    print(f"  Python:      {suite.python_version}")
    print(f"  NumPy:       {suite.numpy_version}")
    print(f"  Duration:    {duration_s}s per benchmark")
    print(f"  Frame size:  {width}x{height}x{DEFAULT_CHANNELS} "
          f"({_frame_size_bytes(width, height, DEFAULT_CHANNELS) / 1024 / 1024:.1f} MB)")
    print(f"  Benchmarks:  {', '.join(sorted(active))}")
    print(f"{'=' * 72}")

    # --- Benchmark 1: SHM Read ---
    if "shm_read" in active:
        print("\n[1/7] Running SHM read speed benchmark...")
        r = bench_shm_read_speed(duration_s, width, height, DEFAULT_CHANNELS)
        suite.results.append(r)
        _print_result(r, verbose)

    # --- Benchmark 1b: SHM Write ---
    if "shm_write" in active:
        print("\n[2/7] Running SHM write speed benchmark...")
        r = bench_shm_write_speed(duration_s, width, height, DEFAULT_CHANNELS)
        suite.results.append(r)
        _print_result(r, verbose)

    # --- Benchmark 2: SCK -> SHM ---
    if "sck_to_shm" in active:
        if skip_sck:
            print("\n[3/7] Skipping SCK -> SHM benchmark (--skip-sck)")
            r = BenchmarkResult(
                name="sck_to_shm_delivery",
                description="Skipped via --skip-sck flag",
                error="Skipped by user request",
            )
            suite.results.append(r)
        else:
            print("\n[3/7] Running SCK -> SHM delivery benchmark (requires screen recording)...")
            r = bench_sck_to_shm(duration_s)
            suite.results.append(r)
            _print_result(r, verbose)

    # --- Benchmark 3: BGRA -> RGB ---
    if "bgra" in active:
        test_resolutions = resolutions or [(width, height)]
        print(f"\n[4/7] Running BGRA->RGB conversion benchmark "
              f"({len(test_resolutions)} resolution(s))...")
        results = bench_bgra_to_rgb(duration_s, test_resolutions)
        for r in results:
            suite.results.append(r)
            _print_result(r, verbose)

    # --- Benchmark 4: dhash ---
    if "dhash" in active:
        print("\n[5/7] Running dhash motion detection benchmark...")
        r = bench_dhash_motion(duration_s, width, height)
        suite.results.append(r)
        _print_result(r, verbose)

    # --- Benchmark 5: Full pipeline ---
    if "pipeline" in active:
        print("\n[6/7] Running end-to-end FramePipeline benchmark (motion ON)...")
        r = await bench_frame_pipeline(
            duration_s, width, height,
            motion_detect=True, motion_threshold=0.05,
        )
        suite.results.append(r)
        _print_result(r, verbose)

        print("\n[6b/7] Running end-to-end FramePipeline benchmark (motion OFF)...")
        r = await bench_frame_pipeline(
            duration_s, width, height,
            motion_detect=False,
        )
        suite.results.append(r)
        _print_result(r, verbose)

    # --- Benchmark 6: numpy frombuffer ---
    if "frombuffer" in active:
        print("\n[7/7] Running numpy.frombuffer + reshape benchmark...")
        r = bench_numpy_frombuffer(duration_s, width, height, DEFAULT_CHANNELS)
        suite.results.append(r)
        _print_result(r, verbose)

    # --- Summary ---
    print(f"\n{'=' * 72}")
    print("  SUMMARY")
    print(f"{'=' * 72}")
    for r in suite.results:
        if r.error and r.error != "Skipped by user request":
            print(f"  [FAIL] {r.name}: {r.error}")
        elif r.error:
            print(f"  [SKIP] {r.name}")
        else:
            print(f"  [OK]   {r.name}: {r.fps:,.0f} fps, "
                  f"{r.latency.median_us:.0f} us median, "
                  f"{r.throughput_mb_s:,.0f} MB/s")
    print(f"{'=' * 72}\n")

    # --- Save JSON ---
    if save_json:
        output_path = Path(save_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(suite.to_dict(), f, indent=2, default=str)
        print(f"Results saved to: {output_path}")

    # Also auto-save to the timestamped output directory
    auto_dir = Path(DEFAULT_OUTPUT_DIR)
    auto_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    auto_path = auto_dir / f"bench_{ts}.json"
    with open(auto_path, "w") as f:
        json.dump(suite.to_dict(), f, indent=2, default=str)
    print(f"Auto-saved to: {auto_path}")

    return suite


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="JARVIS Vision Pipeline Benchmark Suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 tests/benchmarks/vision_benchmarks.py
  python3 tests/benchmarks/vision_benchmarks.py --duration 10 --save-json results.json
  python3 tests/benchmarks/vision_benchmarks.py --skip-sck --only bgra dhash
  python3 tests/benchmarks/vision_benchmarks.py --resolutions 1920x1080 2560x1440 3840x2160
  python3 tests/benchmarks/vision_benchmarks.py --width 2560 --height 1440 --verbose
        """,
    )
    parser.add_argument(
        "--duration", type=float, default=DEFAULT_DURATION_S,
        help=f"Duration in seconds for each benchmark (default: {DEFAULT_DURATION_S})",
    )
    parser.add_argument(
        "--save-json", type=str, default=None,
        help="Path to write JSON results (in addition to auto-save)",
    )
    parser.add_argument(
        "--skip-sck", action="store_true",
        help="Skip SCK -> SHM benchmark (no screen recording permission needed)",
    )
    parser.add_argument(
        "--only", nargs="+", choices=_ALL_BENCHMARKS, default=None,
        help=f"Run only specified benchmarks. Choices: {', '.join(_ALL_BENCHMARKS)}",
    )
    parser.add_argument(
        "--resolutions", nargs="+", type=str, default=None,
        help="Resolutions for BGRA benchmark (e.g., 1920x1080 2560x1440)",
    )
    parser.add_argument(
        "--width", type=int, default=DEFAULT_WIDTH,
        help=f"Default frame width (default: {DEFAULT_WIDTH})",
    )
    parser.add_argument(
        "--height", type=int, default=DEFAULT_HEIGHT,
        help=f"Default frame height (default: {DEFAULT_HEIGHT})",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show detailed metadata for each benchmark",
    )
    parser.add_argument(
        "--log-level", type=str, default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: WARNING)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    resolutions = None
    if args.resolutions:
        resolutions = [_parse_resolution(r) for r in args.resolutions]

    # Handle Ctrl+C gracefully
    signal.signal(signal.SIGINT, lambda *_: (print("\nInterrupted."), sys.exit(130)))

    asyncio.run(
        run_benchmarks(
            duration_s=args.duration,
            skip_sck=args.skip_sck,
            only=args.only,
            resolutions=resolutions,
            verbose=args.verbose,
            save_json=args.save_json,
            width=args.width,
            height=args.height,
        )
    )


if __name__ == "__main__":
    main()
