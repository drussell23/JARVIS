# JARVIS Vision Pipeline: 60fps Architecture

## The 115x Journey: 0.5fps to 57.6fps

### Progression

| Version | FPS | Method | Bottleneck | Date |
|---------|-----|--------|------------|------|
| v1.0 | 0.5 | Quartz CGWindowListCreateImage | 47ms per Quartz call | 2026-03-25 |
| v2.0 | 9.0 | Quartz targeted window capture | CGWindowListCreateImage overhead | 2026-03-26 |
| v3.0 | 14.7 | SHM ring buffer + Quartz fallback | Quartz 47ms tax on SHM miss | 2026-03-26 |
| v4.0 | 17.0 | Pure Python mmap (EXC_GUARD fix) | Safe mmap, no ctypes collision | 2026-03-26 |
| v5.0 | 19.7 | SHM-only (Quartz fallback removed) | Static content throttle | 2026-03-26 |
| v6.0 | 57.6 | SHM-first + FramePipeline integration | Display refresh rate (60Hz) | 2026-03-26 |

### Architecture

```
                         60fps
    macOS Display ──────────────────────────────────────────────
         |
         v
    ScreenCaptureKit (SCK)
    [Dedicated daemon thread with CFRunLoop pump]
    - minimumFrameInterval = 1/60s (16.67ms)
    - queueDepth = 8 (absorbs burst delivery)
    - pixelFormat = BGRA
    - GPU acceleration (Metal, macOS 14+)
         |
         | GCD dispatch_queue (QOS_CLASS_USER_INTERACTIVE)
         v
    SCK Delegate: didOutputSampleBuffer
    [SHM-FIRST path — zero intermediate allocation]
         |
         |  Retina: write_frame_downsampled() — 2x downsample direct to SHM
         |  Non-retina: write_frame() or write_frame_strided()
         v
    SHM Ring Buffer (/jarvis_frame_bridge)
    [5 slots, 128-byte header, atomic latest_index]
    ┌─────────┬─────────┬─────────┬─────────┬─────────┐
    │ Slot 0  │ Slot 1  │ Slot 2  │ Slot 3  │ Slot 4  │
    │ 5.18 MB │ 5.18 MB │ 5.18 MB │ 5.18 MB │ 5.18 MB │
    └─────────┴─────────┴─────────┴─────────┴─────────┘
         |
         | Python mmap (ACCESS_WRITE for coherent reads)
         | numpy.frombuffer() — zero-copy view
         v
    ShmFrameReader.read_latest()
    [474K polls/sec, ~0.002ms per read]
         |
         | BGRA → RGB channel swap (copy — also makes data safe from SHM overwrite)
         v
    FramePipeline (asyncio)
    [Bounded queue, dhash motion detection, adaptive poll backoff]
         |
         v
    Consumers: VisionCortex, VisionActionLoop, BallTracker, VLA inference
```

### Key Design Decisions

#### 1. SHM-First C++ Path
**Problem:** Retina displays produce 2880x1800x4 = 20MB frames. At 60fps that's 1.2GB/s.
Downsampling 2x reduces to 1440x900x4 = 5MB per frame = 300MB/s.

The old path did double-copy:
```
pixel_buffer → frame.data (5MB alloc + copy) → SHM (5MB memcpy) = 600MB/s
```

The new SHM-first path eliminates the intermediate buffer:
```
pixel_buffer → SHM direct via write_frame_downsampled() = 300MB/s
```

50% bandwidth reduction. `frame.data` is only populated for legacy `get_frame()` consumers.

#### 2. SCK in Dedicated Thread
**Problem:** SCK requires a pumped CFRunLoop for its GCD completion handlers.
The asyncio event loop does NOT pump a CFRunLoop. Previous attempts to use SCK
from asyncio hung indefinitely.

**Solution:** SCK runs in a daemon thread with its own CFRunLoop pump thread.
The delegate writes frames to SHM. Python polls SHM from asyncio. Complete
decoupling — no GCD callbacks cross the thread boundary.

#### 3. Ring Buffer (5 Slots)
**Problem:** SCK delivers frames in bursts (content-aware delivery). A single
buffer would lose frames during bursts.

**Solution:** 5-slot ring buffer with atomic `latest_index`. Writer has 4 slots
of headroom before wrapping. At 60fps, that's 67ms of safety window.

Reader always reads the latest complete frame via `latest_index` — never blocks,
never reads a partially-written frame.

#### 4. Adaptive Poll Backoff
**Problem:** Busy-spinning at 474K/s wastes CPU when no new frames are arriving
(static content → SCK throttles to ~20fps).

**Solution:** Adaptive backoff based on consecutive empty reads:
- 0-10 empty reads: `await asyncio.sleep(0)` — yield to event loop
- 10-100 empty reads: `await asyncio.sleep(0.001)` — 1ms sleep
- 100+ empty reads: `await asyncio.sleep(0.01)` — 10ms sleep

Resets to zero on every successful read.

#### 5. BGRA→RGB Copy is Intentional
The `frame_arr[:, :, [2, 1, 0]]` fancy indexing creates a copy. This is correct:
- Converts BGRA to RGB for downstream consumers
- Copies data OUT of the SHM ring slot before the writer can overwrite it
- With 5 ring slots at 60fps, the safety window is 67ms — but the copy makes
  it bulletproof regardless of timing

### Performance Characteristics

| Metric | Value |
|--------|-------|
| Capture FPS | 57.6 (96% of 60fps target) |
| Frame gap (median) | 17.19ms |
| Frame gap (P99) | 20.72ms |
| Jitter (stddev) | 1.24ms |
| SHM poll rate | 474K/s |
| Frame size | 5.18 MB (1440x900 BGRA) |
| SHM throughput | 298 MB/s |
| Ring buffer slots | 5 |
| SHM segment | /jarvis_frame_bridge |
| Min frame gap | 6.69ms (burst: 150fps capable) |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `VISION_CAPTURE_BACKEND` | `auto` | `auto`, `shm`, `sck`, `coregraphics` |
| `VISION_CAPTURE_FPS` | `60` | Target FPS for SCK |
| `VISION_SHM_POLL_SLEEP_S` | `0.001` | Base poll sleep in seconds |
| `VISION_MOTION_THRESHOLD` | `0.05` | dhash change threshold |
| `VISION_MOTION_DEBOUNCE_MS` | `0` | Minimum ms between motion events |
| `VISION_FRAME_QUEUE_SIZE` | `10` | Bounded asyncio queue capacity |

### Files

| File | Role |
|------|------|
| `backend/native_extensions/src/shm_frame_bridge.h` | SHM ring buffer writer (C++) |
| `backend/native_extensions/src/fast_capture_stream.mm` | SCK stream + SHM-first delegate |
| `backend/vision/shm_frame_reader.py` | SHM ring buffer reader (Python) |
| `backend/vision/realtime/frame_pipeline.py` | FramePipeline with SHM capture mode |
| `backend/native_extensions/macos_sck_stream.py` | SCK Python wrapper |
| `tests/bench_shm_60fps.py` | SHM delivery rate benchmark |
| `tests/benchmarks/vision_benchmarks.py` | Comprehensive benchmark suite |
| `notebooks/vision_60fps_progression.ipynb` | Progression tracking notebook |

### Content-Aware Delivery

SCK is content-aware — it only delivers frames when screen pixels change:
- **Static desktop:** ~20fps (compositor updates cursor, clock, etc.)
- **Animation (bouncing ball):** 57.6fps (continuous pixel changes)
- **Video playback:** Expected 60fps (continuous changes)
- **Idle screen:** May drop to 1-5fps (minimal changes)

This is optimal behavior — no wasted compute on identical frames.
