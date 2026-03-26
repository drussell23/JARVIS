#!/usr/bin/env python3
"""
SHM Ring Buffer 60fps Benchmark

Measures the ACTUAL frame delivery rate from SCK → SHM → Python.
Fresh process — no throttle from previous SCK sessions.

Architecture:
  1. Start SCK stream in dedicated thread (target_fps=60, display capture)
  2. Poll SHM ring buffer from main thread at maximum rate
  3. Report actual fps, latency, and gaps

Usage:
    python3 tests/bench_shm_60fps.py [--duration 10] [--target-fps 60] [--display]
"""
from __future__ import annotations

import argparse
import os
import signal
import struct
import sys
import threading
import time
from typing import Optional

import numpy as np

# Add native extensions to path
_ext_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "backend", "native_extensions",
)
sys.path.insert(0, _ext_path)

# Add project root for backend imports
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _root)


def _check_screen_recording_permission() -> bool:
    """Check if screen recording permission is granted."""
    try:
        import Quartz
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
        )
        return windows is not None and len(windows) > 0
    except Exception:
        return False


def _start_sck_thread(
    target_fps: int = 60,
    window_id: int = 0,  # 0 = full display
) -> tuple:
    """Start SCK capture in a dedicated thread. Returns (ready_event, stop_event)."""
    import fast_capture_stream

    ready = threading.Event()
    stop = threading.Event()
    error_msg = [None]  # mutable for thread closure

    def _thread():
        try:
            config = fast_capture_stream.StreamConfig()
            config.target_fps = target_fps
            config.max_buffer_size = 3
            config.output_format = "raw"
            config.use_gpu_acceleration = True
            config.drop_frames_on_overflow = True

            stream = fast_capture_stream.CaptureStream(window_id, config)
            if not stream.start():
                error_msg[0] = "stream.start() returned False"
                print(f"[SCK] {error_msg[0]}")
                return

            print(f"[SCK] Stream started — target {target_fps}fps, window_id={window_id}")
            ready.set()

            # Keep thread alive — SCK delivers frames via delegate → SHM
            # No need to call get_frame() — SHM write happens in the delegate
            while not stop.is_set():
                stop.wait(timeout=0.1)

            stream.stop()
            print("[SCK] Stream stopped")
        except Exception as exc:
            error_msg[0] = str(exc)
            print(f"[SCK] Thread error: {exc}")

    t = threading.Thread(target=_thread, daemon=True, name="sck-bench")
    t.start()
    return ready, stop, error_msg


def run_benchmark(
    duration_s: float = 10.0,
    target_fps: int = 60,
    display_mode: bool = True,
    window_id: int = 0,
):
    """Run the SHM polling benchmark."""
    from backend.vision.shm_frame_reader import ShmFrameReader

    print("=" * 70)
    print(f"  SHM Ring Buffer 60fps Benchmark")
    print(f"  Target: {target_fps}fps | Duration: {duration_s}s")
    print(f"  Capture: {'display' if display_mode else f'window {window_id}'}")
    print("=" * 70)

    # --- Pre-check: Screen recording permission ---
    print("\n[0] Checking screen recording permission...")
    if not _check_screen_recording_permission():
        print(
            "FATAL: Screen recording permission NOT granted.\n\n"
            "  Fix: System Settings > Privacy & Security > Screen Recording\n"
            "        Enable your terminal app (Terminal, iTerm2, Cursor, VS Code)\n"
            "        Then restart the terminal.\n"
        )
        return

    print("    Screen recording: OK")

    # --- Phase 1: Start SCK ---
    print("\n[1] Starting SCK stream...")
    ready, stop, error_msg = _start_sck_thread(target_fps=target_fps, window_id=window_id)

    if not ready.wait(timeout=5.0):
        msg = error_msg[0] or "unknown reason"
        print(f"FATAL: SCK failed to start within 5s ({msg})")
        return

    # Let SCK warm up and write initial frames to SHM
    print("[2] Warming up (2s)...")
    time.sleep(2.0)

    # --- Phase 2: Open SHM reader ---
    print("[3] Opening SHM reader...")
    reader = ShmFrameReader()
    if not reader.open():
        print("FATAL: SHM reader failed to open — is SCK writing to SHM?")
        stop.set()
        return

    print(f"    SHM: {reader.width}x{reader.height}x{reader.channels} "
          f"frame_size={reader.frame_size}")

    # --- Phase 3: Benchmark ---
    print(f"\n[4] Benchmarking for {duration_s}s...")
    print("-" * 70)

    frames_read = 0
    frames_duplicate = 0  # SHM had no new frame
    max_gap_ms = 0.0
    min_gap_ms = float("inf")
    gap_histogram = [0] * 11  # 0-1ms, 1-2ms, ..., 9-10ms, 10+ms
    frame_times = []
    last_frame_time = 0.0
    last_counter = 0

    # Pre-warm: read one frame
    f, c = reader.read_latest()
    if f is not None:
        last_counter = c
        last_frame_time = time.monotonic()
        frames_read = 1

    t_start = time.monotonic()
    t_report = t_start
    polls = 0

    while (time.monotonic() - t_start) < duration_s:
        polls += 1
        frame, counter = reader.read_latest()

        if frame is None:
            frames_duplicate += 1
            # Yield to avoid busy-spin
            time.sleep(0)  # ~0.1ms on macOS
            continue

        now = time.monotonic()
        frames_read += 1

        if last_frame_time > 0:
            gap_ms = (now - last_frame_time) * 1000
            frame_times.append(gap_ms)

            if gap_ms > max_gap_ms:
                max_gap_ms = gap_ms
            if gap_ms < min_gap_ms:
                min_gap_ms = gap_ms

            bucket = min(int(gap_ms), 10)
            gap_histogram[bucket] += 1

        # Check for dropped frames (counter gaps)
        if last_counter > 0 and counter > last_counter + 1:
            skipped = counter - last_counter - 1
            if skipped > 0 and (now - t_report) > 1.0:
                pass  # Track but don't print every skip

        last_frame_time = now
        last_counter = counter

        # Report every 2 seconds
        if (now - t_report) >= 2.0:
            elapsed = now - t_start
            current_fps = frames_read / elapsed
            poll_rate = polls / elapsed
            print(
                f"  {elapsed:5.1f}s | {frames_read:5d} frames | "
                f"{current_fps:5.1f} fps | polls: {poll_rate:.0f}/s | "
                f"dupes: {frames_duplicate}"
            )
            t_report = now

    # --- Phase 4: Results ---
    t_end = time.monotonic()
    elapsed = t_end - t_start

    stop.set()  # Stop SCK thread
    reader.close()

    print("\n" + "=" * 70)
    print("  RESULTS")
    print("=" * 70)

    actual_fps = frames_read / elapsed if elapsed > 0 else 0
    poll_rate = polls / elapsed if elapsed > 0 else 0

    print(f"\n  Duration:     {elapsed:.2f}s")
    print(f"  Frames read:  {frames_read}")
    print(f"  Actual FPS:   {actual_fps:.2f}")
    print(f"  Target FPS:   {target_fps}")
    print(f"  Efficiency:   {(actual_fps / target_fps * 100):.1f}%")
    print(f"\n  Poll rate:    {poll_rate:.0f}/s")
    print(f"  Duplicate polls: {frames_duplicate} ({frames_duplicate / max(polls, 1) * 100:.1f}%)")

    if frame_times:
        arr = np.array(frame_times)
        print(f"\n  Frame gaps (ms):")
        print(f"    Mean:   {np.mean(arr):.2f}")
        print(f"    Median: {np.median(arr):.2f}")
        print(f"    Min:    {np.min(arr):.2f}")
        print(f"    Max:    {np.max(arr):.2f}")
        print(f"    P95:    {np.percentile(arr, 95):.2f}")
        print(f"    P99:    {np.percentile(arr, 99):.2f}")
        print(f"    Stddev: {np.std(arr):.2f}")

        print(f"\n  Gap histogram:")
        for i, count in enumerate(gap_histogram):
            if count > 0:
                bar = "#" * min(count, 50)
                if i < 10:
                    print(f"    {i:2d}-{i+1:2d}ms: {count:5d} {bar}")
                else:
                    print(f"    10+ms:  {count:5d} {bar}")

    # Verdict
    print("\n" + "=" * 70)
    if actual_fps >= target_fps * 0.95:
        print(f"  PASS — {actual_fps:.1f}fps >= 95% of {target_fps}fps target")
    elif actual_fps >= target_fps * 0.8:
        print(f"  CLOSE — {actual_fps:.1f}fps is {(actual_fps/target_fps*100):.0f}% of target")
    elif actual_fps >= 30:
        print(f"  PARTIAL — {actual_fps:.1f}fps (30fps+ but below {target_fps}fps target)")
    else:
        print(f"  NEEDS WORK — {actual_fps:.1f}fps (below 30fps)")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="SHM 60fps Benchmark")
    parser.add_argument("--duration", type=float, default=10.0, help="Test duration in seconds")
    parser.add_argument("--target-fps", type=int, default=60, help="Target FPS")
    parser.add_argument("--display", action="store_true", default=True, help="Full display capture (default)")
    parser.add_argument("--window-id", type=int, default=0, help="Window ID (0=display)")
    args = parser.parse_args()

    # Handle Ctrl+C gracefully
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(130))

    run_benchmark(
        duration_s=args.duration,
        target_fps=args.target_fps,
        display_mode=args.display,
        window_id=args.window_id,
    )


if __name__ == "__main__":
    main()
