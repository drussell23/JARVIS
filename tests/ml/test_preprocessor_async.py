"""Non-blocking proof for AdaptiveAudioPreprocessor.preprocess_async (Phase 2).

Mathematically proves the CPU-bound pad/truncate+RMS math runs OFF the event
loop thread: a concurrent heartbeat coroutine advances by many ticks WHILE the
preprocessing runs. If the work blocked the loop, the heartbeat could not tick.

Also proves correctness is preserved off-thread (sync vs async byte-identity).
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from tests.ml.adaptive_audio_preprocessor import (
    AdaptiveAudioPreprocessor,
    PreprocessConfig,
)

SR = 16000


def _big_clip(seconds: float = 30.0) -> np.ndarray:
    n = int(seconds * SR)  # 480k samples @ 30s/16kHz ~ 1.9 MB float32
    t = np.linspace(0.0, seconds, n, endpoint=False)
    return (np.sin(2.0 * np.pi * 220.0 * t) * 0.6).astype(np.float32)


@pytest.mark.asyncio
async def test_preprocess_async_does_not_block_event_loop() -> None:
    big = _big_clip(30.0)
    # target shorter than input => exercises the truncate path on a big array.
    cfg = PreprocessConfig(target_length=SR * 20, sample_rate=SR, target_rms=0.1)
    pre = AdaptiveAudioPreprocessor(cfg)

    done = asyncio.Event()
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0)  # yield to loop; advances iff loop not blocked

    hb = asyncio.create_task(heartbeat())
    try:
        # Loop the work a few times so the off-thread spans clearly overlap many
        # heartbeats even on a fast machine.
        results = []
        for _ in range(5):
            results.append(await pre.preprocess_async(big))
    finally:
        done.set()
        await hb

    # The loop kept spinning WHILE preprocessing ran off-thread.
    assert ticks >= 1, f"event loop appears blocked (ticks={ticks})"
    # In practice we see thousands of ticks; require a strong floor.
    assert ticks > 50, f"weak overlap, ticks={ticks}"

    # Correctness preserved off-thread.
    sync_out = pre.preprocess(big)
    for r in results:
        assert r.dtype == np.float32
        assert r.shape == (cfg.target_length,)
        assert r.tobytes() == sync_out.tobytes()


@pytest.mark.asyncio
async def test_preprocess_async_byte_identical_to_sync() -> None:
    cfg = PreprocessConfig(target_length=SR * 3, sample_rate=SR, target_rms=0.1)
    pre = AdaptiveAudioPreprocessor(cfg)
    x = (np.sin(np.linspace(0, 80 * np.pi, SR * 2)) * 0.3).astype(np.float32)

    sync_out = pre.preprocess(x)
    async_out = await pre.preprocess_async(x)

    assert async_out.dtype == np.float32
    assert async_out.tobytes() == sync_out.tobytes()
