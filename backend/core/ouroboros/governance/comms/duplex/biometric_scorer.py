"""Production x-vector scorer — the sovereign loop's identity organ.

Final integration (operator authorization 2026-07-19). Scores the
sentry's stitched window against the operator's REAL 192-dim x-vector
enrollment via the speechbrain encoder already mounted at
``~/.jarvis/models/speaker_recognition`` (resolved dynamically — the
same symlink farm the VBIA stack owns).

**Startup Race (mandate 2, solved structurally):** the mic arms at
ignition; the encoder loads ASYNCHRONOUSLY in an executor thread (no
blocking sleeps, mandate 1). A wake-word arriving mid-load routes its
payload into a bounded ``asyncio.Queue`` and the scorer reports state
``BIOMETRIC_WARMUP_WAIT``; the moment the load completes, a drainer
task processes the backlog IN ARRIVAL ORDER — zero payload loss, zero
event-loop blocking. Load FAILURES follow the DeferredCaptureAllocator
discipline (same state vocabulary, same bounded-retry shape, fault
classified — a missing model dir is permanent, a transient OSError
retries).

**Memory hygiene (mandate 2):** the encoder is instantiated
``.eval()`` and EVERY inference — verification and Rolling-Evolution
extraction alike — runs under ``torch.no_grad()``: no gradient tape
can accumulate across 24/7 resident uptime.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

import numpy as np

logger = logging.getLogger("Ouroboros.BiometricScorer")

STATE_LOADING = "BIOMETRIC_WARMUP_WAIT"
STATE_READY = "READY"
STATE_FAILED = "FAILED"


def encoder_dir() -> Optional[Path]:
    """Dynamic resolution: env override → the models namespace the
    VBIA stack already populates. NEVER raises."""
    try:
        env = os.environ.get("JARVIS_SPEAKER_ENCODER_DIR", "").strip()
        if env:
            p = Path(os.path.expanduser(env))
            return p if p.exists() else None
        data = Path(os.path.expanduser(
            os.environ.get("JARVIS_DATA_DIR", "~/.jarvis"),
        ))
        p = data / "models" / "speaker_recognition"
        return p if p.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _warmup_queue_cap() -> int:
    try:
        return max(1, int(os.environ.get("JARVIS_BIOMETRIC_WARMUP_QUEUE", "8")))
    except (TypeError, ValueError):
        return 8


def _warmup_timeout_s() -> float:
    try:
        return max(5.0, float(os.environ.get(
            "JARVIS_BIOMETRIC_WARMUP_TIMEOUT_S", "120",
        )))
    except (TypeError, ValueError):
        return 120.0


class XVectorScorer:
    """Async-loading scorer with the Biometric Deferred-Evaluation
    Queue. Injectable seams: ``load_fn`` (blocking, runs in executor;
    returns the encoder) and ``embed_fn(encoder, window) → np.ndarray``
    — production defaults use speechbrain+torch; tests inject fakes.

    ``verify(window) → (confidence, is_owner)`` is the
    BiometricGateAdapter scorer contract; ``last_embedding`` is the
    Rolling-Evolution seam.
    """

    def __init__(
        self,
        *,
        load_fn: Optional[Callable[[], Any]] = None,
        embed_fn: Optional[Callable[[Any, Any], "np.ndarray"]] = None,
        enrollment_loader: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._load_fn = load_fn or self._default_load
        self._embed_fn = embed_fn or self._default_embed
        self._enrollment_loader = enrollment_loader or self._default_enrollment
        self.state = STATE_LOADING
        self._encoder: Any = None
        self._loaded = asyncio.Event()
        self._queue: "asyncio.Queue" = asyncio.Queue(
            maxsize=_warmup_queue_cap(),
        )
        self._drainer: Optional[asyncio.Task] = None
        self.last_embedding: Optional["np.ndarray"] = None
        self.stats = {
            "queued_during_warmup": 0, "drained": 0, "scored": 0,
            "queue_overflow_dropped": 0,
        }

    # ---- production seams (import-guarded) ----

    @staticmethod
    def _default_load() -> Any:
        d = encoder_dir()
        if d is None:
            raise FileNotFoundError("speaker encoder dir absent")
        from speechbrain.pretrained import EncoderClassifier  # noqa: PLC0415
        enc = EncoderClassifier.from_hparams(
            source=str(d), savedir=str(d), run_opts={"device": "cpu"},
        )
        # eval mode — never training, never dropout jitter.
        enc.eval()
        return enc

    @staticmethod
    def _default_embed(encoder: Any, window: Any) -> "np.ndarray":
        import torch  # noqa: PLC0415
        x = torch.from_numpy(
            np.asarray(window, dtype=np.float32).reshape(1, -1),
        )
        # STRICT no_grad: 24/7 residency must never accumulate tape.
        with torch.no_grad():
            emb = encoder.encode_batch(x)
        return emb.squeeze().cpu().numpy().astype(np.float32)

    @staticmethod
    def _default_enrollment() -> Any:
        from .biometric_evolution import load_enrollment  # noqa: PLC0415
        return load_enrollment()

    # ---- lifecycle ----

    async def start_loading(self) -> None:
        """Fire the executor load; returns IMMEDIATELY (the mic is
        already live). NEVER raises."""
        try:
            loop = asyncio.get_running_loop()

            async def _load() -> None:
                try:
                    self._encoder = await loop.run_in_executor(
                        None, self._load_fn,
                    )
                    self.state = STATE_READY
                    self._loaded.set()
                    logger.info("[BioScorer] encoder READY — draining %d "
                                "warmup payload(s)", self._queue.qsize())
                except FileNotFoundError:
                    self.state = STATE_FAILED     # permanent — no retry
                    self._loaded.set()
                except Exception:  # noqa: BLE001
                    logger.warning("[BioScorer] load failed", exc_info=True)
                    self.state = STATE_FAILED
                    self._loaded.set()

            loop.create_task(_load())
            self._drainer = loop.create_task(self._drain_loop())
        except RuntimeError:
            self.state = STATE_FAILED

    async def _drain_loop(self) -> None:
        """The Deferred-Evaluation drainer: waits for READY, then
        serves the backlog in arrival order, then live requests."""
        try:
            await self._loaded.wait()
            while True:
                window, fut = await self._queue.get()
                if not fut.done():
                    fut.set_result(self._score_now(window))
                self.stats["drained"] += 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[BioScorer] drainer degraded", exc_info=True)

    def _score_now(self, window: Any) -> Tuple[float, bool]:
        """Synchronous scoring path (encoder is READY). Fail-closed."""
        try:
            if self.state != STATE_READY or self._encoder is None:
                return 0.0, False
            emb = self._embed_fn(self._encoder, window)
            self.last_embedding = np.asarray(emb, dtype=np.float32).reshape(-1)
            enrolled = self._enrollment_loader()
            if enrolled is None:
                return 0.0, False
            _sid, _name, baseline = enrolled
            b = np.asarray(baseline, dtype=np.float32).reshape(-1)
            e = self.last_embedding
            if b.shape != e.shape or b.size == 0:
                return 0.0, False
            cos = float(np.dot(b, e) / (
                (np.linalg.norm(b) * np.linalg.norm(e)) + 1e-9
            ))
            conf = max(0.0, min(1.0, (cos + 1.0) / 2.0))
            self.stats["scored"] += 1
            return conf, cos > 0.0
        except Exception:  # noqa: BLE001
            return 0.0, False

    async def verify(self, window: Any) -> Tuple[float, bool]:
        """The BiometricGateAdapter scorer contract. Mid-warmup: the
        payload is QUEUED (never dropped, never blocking) and the
        caller's await resolves when the drainer reaches it — bounded
        by the warmup timeout, fail-closed on expiry/overflow."""
        try:
            if self.state == STATE_READY:
                return self._score_now(window)
            if self.state == STATE_FAILED:
                return 0.0, False
            fut: "asyncio.Future" = asyncio.get_running_loop().create_future()
            try:
                self._queue.put_nowait((window, fut))
                self.stats["queued_during_warmup"] += 1
            except asyncio.QueueFull:
                self.stats["queue_overflow_dropped"] += 1
                return 0.0, False
            return await asyncio.wait_for(fut, timeout=_warmup_timeout_s())
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return 0.0, False

    async def stop(self) -> None:
        """Teardown. NEVER raises."""
        try:
            task = self._drainer
            self._drainer = None
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "STATE_FAILED",
    "STATE_LOADING",
    "STATE_READY",
    "XVectorScorer",
    "encoder_dir",
]
