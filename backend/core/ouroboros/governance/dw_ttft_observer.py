"""Phase 12.2 Slice B — TTFT Observer + Dynamic Promotion Math.

Per-``model_id`` rolling TTFT (Time-To-First-Token) sample tracker.
Two consumers, one observer:

  1. **Dynamic promotion** (operator directive 2026-04-27): replaces
     the rejected hardcoded ``JARVIS_DW_PROMOTION_MIN_SUCCESSES=N``
     count-gate. A model graduates from SPECULATIVE to BACKGROUND when
     its TTFT consistency proves it's stably in warm VRAM —
     irrespective of whether that took 3 samples or 12.

  2. **Cold-storage detection**: a TTFT sample > 2σ above the moving
     mean is mathematical proof the model's weights have been evicted
     from active VRAM and are loading from NVMe SSD. The classifier
     temporarily routes the model to SPECULATIVE until its TTFT
     normalizes (auto-recovery on next stable observation).

The math (NO hardcoded integers):

  CV     = stddev / mean                # coefficient of variation
  SEM    = stddev / sqrt(N)             # standard error of the mean
  rel_SEM = SEM / mean = CV / sqrt(N)

  Promotion gate: CV < cv_threshold AND rel_SEM < rel_sem_threshold
    The required N derives mathematically:
      rel_SEM < threshold
      ⇔ CV / sqrt(N) < threshold
      ⇔ N > (CV / threshold)^2
    For a stable model (CV=0.10, threshold=0.05): graduates at N≥4.
    For a noisy model (CV=0.20, threshold=0.05): needs N≥16, but
    likely fails the CV<0.15 gate first.

  Cold-storage gate: latest > mean + sigma_mult * stddev
    Soft floor: N >= 3 (statistical floor for sample stddev to be
    non-degenerate; below that, stddev is mathematically meaningless).
    NOT a tuning parameter — the floor is dictated by the definition
    of sample variance.

Authority surface:
  * ``TtftSample``, ``TtftStats`` — frozen dataclasses
  * ``TtftObserver`` — record_ttft / stats / is_promotion_ready /
    is_cold_storage / promotion_ready_models / cold_storage_models
  * ``tracking_enabled()`` — re-read at call time

NEVER raises out of any public method.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master flag + tunables
# ---------------------------------------------------------------------------


def tracking_enabled() -> bool:
    """``JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED`` (default ``true`` —
    graduated in Phase 12.2 Slice E).

    Re-read at call time so monkeypatch works in tests + operators
    can flip live without re-init. Hot-revert path: ``export
    JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED=false`` returns the
    observer to dormant — record_ttft becomes a no-op + the
    promotion / cold-storage gates short-circuit to False."""
    raw = os.environ.get(
        "JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


def ttft_demotion_enabled() -> bool:
    """``JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED`` (default ``true`` —
    graduated in Phase 12.2 Slice E).

    Phase 12.2 Slice C master flag. When ``true``:

      * ``PromotionLedger.is_eligible_for_promotion`` consults
        ``observer.is_promotion_ready(model_id)`` instead of the
        legacy count gate — N derives mathematically from observed CV.
      * ``DwCatalogClassifier`` consults ``observer.is_cold_storage``
        as a soft gate — cold-storage models temporarily route to
        SPECULATIVE only until TTFT normalizes (auto-recovery).

    Hot-revert path: ``export JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED=
    false`` returns the gate to legacy count-based behavior +
    classifier ignores cold-storage signal. Independent from
    ``tracking_enabled()`` — operators can disable acting on TTFT
    without losing the observation stream."""
    raw = os.environ.get(
        "JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


def _cv_threshold() -> float:
    """``JARVIS_TOPOLOGY_TTFT_CV_THRESHOLD`` (default 0.15).

    Coefficient of variation gate for promotion. A model with CV<0.15
    has TTFT noise <= 15% of its mean — consistent enough to graduate
    from SPECULATIVE quarantine."""
    try:
        return float(
            os.environ.get(
                "JARVIS_TOPOLOGY_TTFT_CV_THRESHOLD", "0.15",
            ).strip()
        )
    except (ValueError, TypeError):
        return 0.15


def _rel_sem_threshold() -> float:
    """``JARVIS_TOPOLOGY_TTFT_REL_SEM_THRESHOLD`` (default 0.05).

    Standard-error-of-the-mean threshold (relative to mean). Together
    with CV gate, derives the required minimum N:
      rel_SEM = CV / sqrt(N) < threshold
      ⇒ N > (CV / threshold)^2
    Tighter threshold → more samples required for confident promotion."""
    try:
        return float(
            os.environ.get(
                "JARVIS_TOPOLOGY_TTFT_REL_SEM_THRESHOLD", "0.05",
            ).strip()
        )
    except (ValueError, TypeError):
        return 0.05


def _promotion_ceiling_ms() -> int:
    """``JARVIS_TOPOLOGY_TTFT_PROMOTION_CEILING_MS`` (default 5000).

    Phase 12.2 Slice G — Absolute Ceiling Gate. A model whose mean
    TTFT exceeds this ceiling is **functionally dead** regardless of
    how tight its variance is. Mathematical stability (low CV, low
    rel_SEM) is necessary but NOT sufficient for promotion — the
    mean itself must indicate the model is actually responsive.

    Operator directive 2026-04-28: a model returning uniform 30-second
    timeouts has CV=0 and rel_SEM=0, which would pass the variance
    gates trivially, but it is not warm — it is dead. The absolute
    ceiling fires BEFORE variance math to reject this class of false
    positive.

    5000ms default chosen to be generous: a typical warm-VRAM DW model
    returns first chunk in <500ms; a model loading from cold NVMe can
    take 2-3s; anything above 5s is so far outside the warm distribution
    that promotion would be a routing error. Operators can tune via
    env if their endpoint has different latency characteristics."""
    try:
        return max(1, int(
            os.environ.get(
                "JARVIS_TOPOLOGY_TTFT_PROMOTION_CEILING_MS", "5000",
            ).strip()
        ))
    except (ValueError, TypeError):
        return 5000


def _cold_sigma() -> float:
    """``JARVIS_TOPOLOGY_TTFT_COLD_SIGMA`` (default 2.0).

    σ multiplier for cold-storage detection. ``latest > mean + Kσ``
    triggers demotion. K=2.0 corresponds to a ~2.5% false-positive
    rate under Gaussian assumption, but TTFT distributions can be
    long-tailed so a real cold-storage spike is multiple σ — well
    above the threshold."""
    try:
        return float(
            os.environ.get(
                "JARVIS_TOPOLOGY_TTFT_COLD_SIGMA", "2.0",
            ).strip()
        )
    except (ValueError, TypeError):
        return 2.0


def _window_n() -> int:
    """``JARVIS_TOPOLOGY_TTFT_WINDOW_N`` (default 50).

    Maximum samples retained per model_id. Older samples evicted
    on overflow. Bounds memory + makes the rolling stats responsive
    to recent changes (stale data from yesterday shouldn't dominate
    today's promotion decision).

    NOT a hardcoded promotion gate — it's a memory bound. Promotion
    derives N from the mathematical formula above; window_n only caps
    the maximum N the formula can see."""
    try:
        return max(2, int(
            os.environ.get(
                "JARVIS_TOPOLOGY_TTFT_WINDOW_N", "50",
            ).strip()
        ))
    except (ValueError, TypeError):
        return 50


def _state_path() -> Path:
    """``JARVIS_TOPOLOGY_TTFT_STATE_PATH`` (default
    ``.jarvis/dw_ttft_observer.json``). Override for tests."""
    raw = os.environ.get(
        "JARVIS_TOPOLOGY_TTFT_STATE_PATH",
        ".jarvis/dw_ttft_observer.json",
    ).strip()
    return Path(raw)


# Mathematical floor for sample variance to be non-degenerate.
# Sample stddev with N=1 is 0; with N=2 it has standard error equal
# to itself. N=3 is the absolute minimum for the 2σ comparison to
# return anything meaningful. NOT a tuning parameter — this is the
# definition of sample variance, not a hardcoded threshold.
_MIN_N_FOR_NONDEGENERATE_VARIANCE = 3


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


SCHEMA_VERSION = "ttft_observer.1"


@dataclass(frozen=True)
class TtftSample:
    """One TTFT measurement. Frozen + hashable for safe inter-thread
    sharing and easy diff against prior samples."""
    model_id: str
    ttft_ms: int
    sample_unix: float
    op_id: str = ""


@dataclass(frozen=True)
class TtftStats:
    """Computed statistics over a model's rolling sample window.
    All fields are derived from ``samples`` — no hidden state."""
    model_id: str
    n: int
    mean_ms: float
    stddev_ms: float
    cv: float                  # stddev / mean (coefficient of variation)
    rel_sem: float             # CV / sqrt(N) (relative SEM)
    latest_ms: int             # most recent sample (for cold detection)
    window_start_unix: float   # oldest retained sample timestamp
    window_end_unix: float     # newest sample timestamp


# ---------------------------------------------------------------------------
# Atomic disk I/O
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Observer
# ---------------------------------------------------------------------------


class TtftObserver:
    """Per-model rolling TTFT sample tracker.

    Pure observer — does NOT route, does NOT demote, does NOT
    promote. Emits READ-ONLY signals (``is_promotion_ready``,
    ``is_cold_storage``) that downstream consumers (PromotionLedger,
    DwCatalogClassifier) read as gates.

    Thread-safe via ``RLock``. Persistence via atomic temp+rename.
    NEVER raises out of any public method.
    """

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        autosave: bool = True,
    ) -> None:
        self._path = path
        self._autosave = autosave
        self._samples: Dict[str, Deque[TtftSample]] = {}
        self._lock = threading.RLock()
        self._loaded = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _resolved_path(self) -> Path:
        return self._path if self._path is not None else _state_path()

    def load(self) -> None:
        """Load samples from disk. Missing file = empty buffer; corrupt
        = log warn + start empty. NEVER raises."""
        with self._lock:
            self._loaded = True
            p = self._resolved_path()
            if not p.exists():
                return
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "[TtftObserver] corrupt state at %s — starting "
                    "empty (%s)", p, exc,
                )
                return
            if not isinstance(payload, Mapping):
                return
            if payload.get("schema_version") != SCHEMA_VERSION:
                logger.warning(
                    "[TtftObserver] schema mismatch at %s "
                    "(found=%r expected=%r) — starting empty",
                    p, payload.get("schema_version"), SCHEMA_VERSION,
                )
                return
            samples_raw = payload.get("samples", {})
            if not isinstance(samples_raw, Mapping):
                return
            cap = _window_n()
            for mid, raw_list in samples_raw.items():
                if not isinstance(mid, str) or not isinstance(raw_list, list):
                    continue
                buf: Deque[TtftSample] = deque(maxlen=cap)
                for r in raw_list:
                    if not isinstance(r, Mapping):
                        continue
                    try:
                        buf.append(TtftSample(
                            model_id=mid,
                            ttft_ms=int(r.get("ttft_ms", 0)),
                            sample_unix=float(
                                r.get("sample_unix", 0.0) or 0.0,
                            ),
                            op_id=str(r.get("op_id", "")),
                        ))
                    except (ValueError, TypeError):
                        continue
                if buf:
                    self._samples[mid] = buf

    def save(self) -> None:
        """Write all sample buffers to disk atomically. NEVER raises."""
        with self._lock:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "samples": {
                    mid: [
                        {
                            "ttft_ms": s.ttft_ms,
                            "sample_unix": s.sample_unix,
                            "op_id": s.op_id,
                        }
                        for s in buf
                    ]
                    for mid, buf in self._samples.items()
                },
            }
            try:
                _atomic_write(
                    self._resolved_path(),
                    json.dumps(payload, sort_keys=True, indent=2),
                )
            except OSError as exc:
                logger.warning(
                    "[TtftObserver] save failed: %s — state remains in "
                    "memory", exc,
                )

    def _maybe_autosave(self) -> None:
        if self._autosave:
            self.save()

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ------------------------------------------------------------------
    # Telemetry input
    # ------------------------------------------------------------------

    def record_ttft(
        self,
        model_id: str,
        ttft_ms: int,
        op_id: str = "",
    ) -> None:
        """Record one TTFT measurement. NEVER raises on garbage input."""
        if not model_id or not model_id.strip():
            return
        try:
            ms = int(ttft_ms)
        except (ValueError, TypeError):
            return
        if ms < 0:
            return  # negative TTFT is nonsensical
        self._ensure_loaded()
        with self._lock:
            buf = self._samples.get(model_id)
            if buf is None:
                buf = deque(maxlen=_window_n())
                self._samples[model_id] = buf
            elif buf.maxlen != _window_n():
                # Window was resized via env override since last record.
                # Rebuild the deque with the new bound, preserving
                # most-recent-first semantics.
                new_buf: Deque[TtftSample] = deque(
                    list(buf)[-_window_n():], maxlen=_window_n(),
                )
                buf = new_buf
                self._samples[model_id] = buf
            buf.append(TtftSample(
                model_id=model_id,
                ttft_ms=ms,
                sample_unix=time.time(),
                op_id=str(op_id or ""),
            ))
            self._maybe_autosave()

    def clear(self, model_id: str) -> None:
        """Drop all samples for ``model_id``. Used by sentinel on
        catalog refresh + by operator-driven reset paths. NEVER raises."""
        if not model_id or not model_id.strip():
            return
        self._ensure_loaded()
        with self._lock:
            if model_id in self._samples:
                del self._samples[model_id]
                self._maybe_autosave()

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def stats(self, model_id: str) -> Optional[TtftStats]:
        """Compute rolling stats over the current window. Returns
        ``None`` for unknown / empty buffers. NEVER raises."""
        if not model_id or not model_id.strip():
            return None
        self._ensure_loaded()
        with self._lock:
            buf = self._samples.get(model_id)
            if not buf:
                return None
            samples_list = list(buf)
        # Compute outside the lock — stats are pure functions of samples
        n = len(samples_list)
        ttfts = [s.ttft_ms for s in samples_list]
        mean = sum(ttfts) / n if n > 0 else 0.0
        if n >= 2:
            # Sample stddev (Bessel's correction: divide by N-1)
            variance = sum((x - mean) ** 2 for x in ttfts) / (n - 1)
            stddev = math.sqrt(max(0.0, variance))
        else:
            stddev = 0.0
        cv = (stddev / mean) if mean > 0 else 0.0
        # Relative SEM = (stddev/sqrt(N)) / mean = CV / sqrt(N)
        rel_sem = (cv / math.sqrt(n)) if n > 0 else 0.0
        return TtftStats(
            model_id=model_id,
            n=n,
            mean_ms=mean,
            stddev_ms=stddev,
            cv=cv,
            rel_sem=rel_sem,
            latest_ms=samples_list[-1].ttft_ms,
            window_start_unix=samples_list[0].sample_unix,
            window_end_unix=samples_list[-1].sample_unix,
        )

    # ------------------------------------------------------------------
    # Promotion gate (operator directive 2026-04-27 — math, not count)
    # ------------------------------------------------------------------

    def is_promotion_ready(self, model_id: str) -> bool:
        """True iff ``model_id`` has demonstrated TTFT consistency
        sufficient for graduation from SPECULATIVE to BACKGROUND.

        Three gates, all must hold:

          * **Absolute ceiling gate** (Phase 12.2 Slice G): mean_ms <
            promotion_ceiling_ms. A model whose mean TTFT exceeds the
            ceiling is functionally dead — uniform 30-second timeouts
            have CV=0 (trivially "consistent") but are not warm. The
            ceiling rejects this class of false positive BEFORE any
            variance math runs. Mathematical stability is necessary
            but NOT sufficient — the mean itself must indicate the
            model is actually responsive.

          * **Coefficient of variation gate**: CV < cv_threshold.
            The model's TTFT noise (1σ) is bounded relative to its
            mean. A model that varies wildly is not yet "in warm
            VRAM" by the operator's definition.

          * **Relative SEM gate**: rel_SEM < sem_threshold.
            We have enough samples for our mean estimate to be
            trustworthy. Mathematically equivalent to:
              N > (CV / sem_threshold)^2

        Together they encode: "the mean is stable AND below the
        responsiveness ceiling AND the model is consistent." NO
        hardcoded count required — the math derives N dynamically
        from observed CV. A consistent model graduates with few
        samples; a noisy model needs more.

        NEVER raises."""
        if not tracking_enabled():
            return False
        s = self.stats(model_id)
        if s is None:
            return False
        if s.mean_ms <= 0 or s.n < 2:
            return False
        # Slice G — Absolute Ceiling Gate (operator directive 2026-04-28).
        # Fires BEFORE variance math so a uniform-timeout false positive
        # cannot pass through the CV gate. Critical: a model returning
        # 30s timeouts has CV=0 but is not warm — it is dead.
        if s.mean_ms >= _promotion_ceiling_ms():
            return False
        return s.cv < _cv_threshold() and s.rel_sem < _rel_sem_threshold()

    def promotion_ready_models(self) -> Tuple[str, ...]:
        """Snapshot of model_ids currently passing the promotion gate.
        Useful for the discovery runner to bulk-promote at refresh.
        NEVER raises."""
        if not tracking_enabled():
            return ()
        self._ensure_loaded()
        with self._lock:
            mids = list(self._samples.keys())
        ready = [mid for mid in mids if self.is_promotion_ready(mid)]
        return tuple(sorted(ready))

    # ------------------------------------------------------------------
    # Cold-storage gate
    # ------------------------------------------------------------------

    def is_cold_storage(self, model_id: str) -> bool:
        """True iff the most recent TTFT for ``model_id`` is
        ``cold_sigma * stddev`` above the rolling mean — strong
        evidence the model's weights have just been loaded from cold
        storage (NVMe SSD) into active VRAM.

        Statistical floor: requires N >= 3 to make the σ comparison
        non-degenerate. Below that, ``stddev`` is too noisy to be
        meaningful. This is mathematical fact, not a tuning parameter.

        NEVER raises."""
        if not tracking_enabled():
            return False
        s = self.stats(model_id)
        if s is None:
            return False
        if s.n < _MIN_N_FOR_NONDEGENERATE_VARIANCE:
            return False
        if s.stddev_ms <= 0:
            return False
        threshold_ms = s.mean_ms + _cold_sigma() * s.stddev_ms
        return s.latest_ms > threshold_ms

    def cold_storage_models(self) -> Tuple[str, ...]:
        """Snapshot of model_ids currently in cold-storage state.
        NEVER raises."""
        if not tracking_enabled():
            return ()
        self._ensure_loaded()
        with self._lock:
            mids = list(self._samples.keys())
        cold = [mid for mid in mids if self.is_cold_storage(mid)]
        return tuple(sorted(cold))

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def all_tracked_models(self) -> Tuple[str, ...]:
        self._ensure_loaded()
        with self._lock:
            return tuple(sorted(self._samples.keys()))

    def sample_count(self, model_id: str) -> int:
        if not model_id or not model_id.strip():
            return 0
        self._ensure_loaded()
        with self._lock:
            buf = self._samples.get(model_id)
            return len(buf) if buf else 0


__all__ = [
    "SCHEMA_VERSION",
    "TtftObserver",
    "TtftSample",
    "TtftStats",
    "tracking_enabled",
    "ttft_demotion_enabled",
]
