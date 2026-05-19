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

# Predictive Provider Resilience Arc — Slice 0 (Observability Seam).
# A SEPARATE schema for the provider-latency sample stream. The
# existing ``SCHEMA_VERSION`` above (and the TtftSample promotion
# data it persists) is byte-untouched — Slice 0 is strictly additive
# so the DW dynamic-promotion path has zero behavioural drift.
PROVIDER_LATENCY_SCHEMA_VERSION = "provider_latency.1"


def _provider_latency_window_n() -> int:
    """``JARVIS_PROVIDER_LATENCY_WINDOW_N`` (default 200).

    Hard upper bound on ProviderLatencySample retained per
    ``provider`` key in the in-memory ring. Pure memory bound —
    respects the OOM-hardening boundaries locked in 2026-05-19
    (deque(maxlen=N), drop-oldest, no unbounded growth). The
    Slice-1 forecaster derives its EMA window mathematically; this
    only caps the maximum N the ring can hold."""
    try:
        return max(2, int(
            os.environ.get(
                "JARVIS_PROVIDER_LATENCY_WINDOW_N", "200",
            ).strip()
        ))
    except (ValueError, TypeError):
        return 200


def provider_latency_forecast_enabled() -> bool:
    """``JARVIS_PROVIDER_LATENCY_FORECAST_ENABLED`` (Slice 1 master,
    default ``false``). Re-read at call time. SHADOW-ONLY even when
    true — the forecaster logs predicted-vs-actual + MAE and NEVER
    enforces a timeout or triggers shedding (that is Slice 2/3)."""
    raw = os.environ.get(
        "JARVIS_PROVIDER_LATENCY_FORECAST_ENABLED", "false",
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _forecast_alpha() -> float:
    """``JARVIS_PROVIDER_LATENCY_FORECAST_ALPHA`` (default 0.2).

    EMA smoothing factor for the streaming-moment regression.
    Bounded to ``(0, 1]`` — closer to 1 = more reactive to recent
    latency, closer to 0 = smoother. NOT a hardcoded model
    parameter: it is the single recency knob; the slope/intercept
    are DERIVED from the data, never set."""
    try:
        a = float(
            os.environ.get(
                "JARVIS_PROVIDER_LATENCY_FORECAST_ALPHA", "0.2",
            ).strip()
        )
    except (ValueError, TypeError):
        return 0.2
    if not (0.0 < a <= 1.0):
        return 0.2
    return a


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


@dataclass(frozen=True)
class ProviderLatencySample:
    """One provider-call latency observation — the raw training row
    the Slice-1 TTFT Forecaster regresses on.

    Frozen + hashable. ``input_tokens`` is the provider's OWN
    server-side tokenizer count (Claude ``usage.input_tokens`` /
    DoubleWord ``CompleteSyncResult.input_tokens``) — the most
    precise possible measure of the model tier's true input size,
    NOT a ``len(chars)/4`` client estimate.

    ``ttft_ms`` = time-to-first-token (``-1`` when no byte ever
    arrived — the connect/LB-timeout signature). ``total_ms`` =
    full provider-call wall time. ``outcome`` is a free string
    (``success`` / ``timeout`` / ``cancelled`` / ``error``)."""
    provider: str
    route: str
    op_id: str
    input_tokens: int
    ttft_ms: int
    total_ms: int
    outcome: str
    sample_unix: float

    def to_jsonl_obj(self) -> Dict[str, Any]:
        """Stable dict for the cross-process JSONL dataset. Key
        order is fixed so the Slice-1 forecaster parses positionally
        or by key without schema drift."""
        return {
            "schema_version": PROVIDER_LATENCY_SCHEMA_VERSION,
            "provider": self.provider,
            "route": self.route,
            "op_id": self.op_id,
            "input_tokens": self.input_tokens,
            "ttft_ms": self.ttft_ms,
            "total_ms": self.total_ms,
            "outcome": self.outcome,
            "sample_unix": self.sample_unix,
        }


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
        # Slice 0 — provider-latency ring. SEPARATE keyed deque dict
        # so the existing TtftSample promotion buffers + their
        # persisted schema are byte-untouched (zero behavioural
        # drift on the DW dynamic-promotion path). Shares the same
        # RLock + drop-oldest bounded-deque discipline.
        self._latency_samples: Dict[str, Deque[ProviderLatencySample]] = {}
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

    # ------------------------------------------------------------------
    # Slice 0 — Provider-latency ring (Predictive Provider Resilience)
    # ------------------------------------------------------------------

    def record_provider_latency(
        self,
        sample: ProviderLatencySample,
    ) -> None:
        """Append one ProviderLatencySample into the bounded ring,
        keyed by ``provider``. Pure observer — records and returns.
        NEVER raises on garbage input (mirrors ``record_ttft``).

        Memory bound: each per-provider deque is created with
        ``maxlen=_provider_latency_window_n()`` so it physically
        cannot grow unbounded (drop-oldest on overflow). Does NOT
        autosave to the TTFT promotion JSON — durable persistence
        is the caller's cross-process JSONL append (Slice 0
        contract), keeping the existing promotion schema untouched."""
        # isinstance also rejects None — single defensive guard,
        # NEVER-raises contract (mirrors record_ttft house style).
        if not isinstance(sample, ProviderLatencySample):
            return
        try:
            key = str(sample.provider or "").strip()
            if not key:
                return
            cap = _provider_latency_window_n()
            with self._lock:
                buf = self._latency_samples.get(key)
                if buf is None:
                    buf = deque(maxlen=cap)
                    self._latency_samples[key] = buf
                elif buf.maxlen != cap:
                    # Window resized via env override since last record:
                    # rebuild preserving most-recent, keep the bound.
                    buf = deque(list(buf)[-cap:], maxlen=cap)
                    self._latency_samples[key] = buf
                buf.append(sample)
        except Exception:  # noqa: BLE001 — observer NEVER raises
            return

    def provider_latency_samples(
        self,
        provider: str,
    ) -> Tuple[ProviderLatencySample, ...]:
        """Immutable snapshot of the current ring for ``provider``
        (oldest→newest). Empty tuple for unknown keys. NEVER raises.
        This is the read surface the Slice-1 forecaster consumes."""
        if not provider or not provider.strip():
            return ()
        try:
            with self._lock:
                buf = self._latency_samples.get(provider.strip())
                return tuple(buf) if buf else ()
        except Exception:  # noqa: BLE001
            return ()

    def provider_latency_sample_count(self, provider: str) -> int:
        """Number of retained samples for ``provider``. NEVER raises."""
        if not provider or not provider.strip():
            return 0
        try:
            with self._lock:
                buf = self._latency_samples.get(provider.strip())
                return len(buf) if buf else 0
        except Exception:  # noqa: BLE001
            return 0

    def all_latency_providers(self) -> Tuple[str, ...]:
        """Sorted provider keys with at least one latency sample."""
        try:
            with self._lock:
                return tuple(sorted(self._latency_samples.keys()))
        except Exception:  # noqa: BLE001
            return ()

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


# ---------------------------------------------------------------------------
# Slice 1 — TTFT Forecaster (SHADOW MODE)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForecastResult:
    """One shadow forecast outcome (predicted vs actual). Frozen.

    ``predicted_ms`` is ``None`` when the per-key model has fewer
    than the non-degenerate-variance floor of samples (the math
    refuses to extrapolate a slope it cannot yet estimate — NOT a
    hardcoded gate, the definition of regression validity)."""
    provider: str
    route: str
    input_tokens: int
    predicted_ms: Optional[float]
    actual_ms: int
    abs_err_ms: Optional[float]
    mae_ms: Optional[float]
    n: int


class _RegState:
    """EMA-weighted streaming-moment accumulator for ONE
    ``(provider, route)`` key. Holds recency-weighted moments of
    ``x = input_tokens`` and ``y = ttft_ms`` so an ordinary
    least-squares line can be read off WITHOUT storing samples:

        slope     = (E[xy] − E[x]E[y]) / (E[x²] − E[x]²)
        intercept =  E[y] − slope · E[x]

    All four moments decay by ``alpha`` each observation, so the
    fit tracks reality and forgets stale regimes. No coefficient is
    ever hardcoded — slope/intercept are pure functions of observed
    data."""

    __slots__ = ("ex", "ey", "exx", "exy", "n", "_mae_ema", "_mae_n")

    def __init__(self) -> None:
        self.ex = 0.0
        self.ey = 0.0
        self.exx = 0.0
        self.exy = 0.0
        self.n = 0
        self._mae_ema: Optional[float] = None
        self._mae_n = 0

    def predict(self, x: float) -> Optional[float]:
        """OLS prediction from current EMA moments, or ``None`` if
        the slope is not yet estimable (need ≥ the variance floor
        AND non-degenerate x-variance)."""
        if self.n < _MIN_N_FOR_NONDEGENERATE_VARIANCE:
            return None
        var_x = self.exx - self.ex * self.ex
        if var_x <= 1e-9:
            # All observed payloads identical so far — slope
            # undefined; fall back to the mean response (still a
            # data-derived prediction, not a constant).
            return self.ey
        slope = (self.exy - self.ex * self.ey) / var_x
        intercept = self.ey - slope * self.ex
        pred = intercept + slope * x
        return pred if pred >= 0.0 else 0.0

    def update(self, x: float, y: float, alpha: float) -> None:
        """Fold one observation into the EMA moments AFTER it has
        been scored (prequential / predict-then-update)."""
        if self.n == 0:
            self.ex, self.ey = x, y
            self.exx, self.exy = x * x, x * y
        else:
            self.ex += alpha * (x - self.ex)
            self.ey += alpha * (y - self.ey)
            self.exx += alpha * (x * x - self.exx)
            self.exy += alpha * (x * y - self.exy)
        self.n += 1

    def record_error(self, abs_err: float, alpha: float) -> None:
        if self._mae_ema is None:
            self._mae_ema = abs_err
        else:
            self._mae_ema += alpha * (abs_err - self._mae_ema)
        self._mae_n += 1

    @property
    def mae(self) -> Optional[float]:
        return self._mae_ema


class TtftForecaster:
    """Predictive TTFT model — SHADOW ONLY.

    Composes the existing latency stream: it does NOT store its own
    samples, it folds each observed ``ProviderLatencySample`` into a
    tiny per-key :class:`_RegState`. Public surface:

      * ``forecast(provider, route, input_tokens)`` → predicted ms
        (or ``None`` before the model is estimable);
      * ``observe(sample)`` → prequential step: score the standing
        prediction against the just-arrived actual, accumulate MAE,
        THEN update the model (honest out-of-sample evaluation —
        the model never sees a sample before predicting it);
      * ``warm_start_from_jsonl(path)`` → replay the durable
        Slice-0 dataset so the forecaster is not cold on boot.

    STRICT SHADOW CONTRACT: this class only computes + the caller
    only logs. It NEVER returns a timeout, never mutates a client,
    never triggers shedding. Enforcement is Slice 2/3. NEVER raises
    out of any public method."""

    def __init__(self) -> None:
        self._states: Dict[str, _RegState] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(provider: str, route: str) -> str:
        return f"{(provider or '').strip()}|{(route or '').strip()}"

    def forecast(
        self, provider: str, route: str, input_tokens: int,
    ) -> Optional[float]:
        """Current-model TTFT prediction in ms. ``None`` until the
        per-key regression is estimable. NEVER raises."""
        try:
            with self._lock:
                st = self._states.get(self._key(provider, route))
                if st is None:
                    return None
                return st.predict(float(max(0, int(input_tokens))))
        except Exception:  # noqa: BLE001 — forecaster never raises
            return None

    def observe(self, sample: "ProviderLatencySample") -> ForecastResult:
        """Prequential step. Predict with the STANDING model, score
        vs ``sample.ttft_ms``, accumulate MAE, then fold the sample
        in. Only meaningful for streaming successes (ttft_ms ≥ 0 and
        input_tokens > 0) — degenerate timeout rows (ttft=-1) are
        recorded as observations of nothing and skipped from the
        regression so they cannot poison the slope. NEVER raises."""
        try:
            if not isinstance(sample, ProviderLatencySample):
                return ForecastResult("", "", 0, None, 0, None, None, 0)
            prov = str(sample.provider or "")
            route = str(sample.route or "")
            x = float(max(0, int(sample.input_tokens)))
            y = int(sample.ttft_ms)
            key = self._key(prov, route)
            alpha = _forecast_alpha()
            with self._lock:
                st = self._states.get(key)
                if st is None:
                    st = _RegState()
                    self._states[key] = st
                # Skip non-fittable rows (timeout/cancel: ttft=-1, or
                # zero-token) — predicting/fitting on them would
                # corrupt the EMA. Still returns a result row so the
                # caller can log the skip transparently.
                if y < 0 or x <= 0.0 or sample.outcome != "success":
                    return ForecastResult(
                        prov, route, int(x), None, max(0, y),
                        None, st.mae, st.n,
                    )
                pred = st.predict(x)
                abs_err = abs(pred - y) if pred is not None else None
                if abs_err is not None:
                    st.record_error(abs_err, alpha)
                st.update(x, float(y), alpha)
                return ForecastResult(
                    prov, route, int(x), pred, y, abs_err,
                    st.mae, st.n,
                )
        except Exception:  # noqa: BLE001 — forecaster never raises
            return ForecastResult("", "", 0, None, 0, None, None, 0)

    def warm_start_from_jsonl(self, path: Path) -> int:
        """Replay the durable Slice-0 JSONL into the model so it is
        not cold on boot. Idempotent-safe to call once. Returns the
        number of rows folded. NEVER raises (bad lines skipped)."""
        n = 0
        try:
            p = Path(path)
            if not p.exists():
                return 0
            for line in p.read_text(
                encoding="utf-8", errors="replace",
            ).splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(r, Mapping):
                    continue
                try:
                    self.observe(ProviderLatencySample(
                        provider=str(r.get("provider", "")),
                        route=str(r.get("route", "")),
                        op_id=str(r.get("op_id", "")),
                        input_tokens=int(r.get("input_tokens", 0) or 0),
                        ttft_ms=int(r.get("ttft_ms", -1)),
                        total_ms=int(r.get("total_ms", 0) or 0),
                        outcome=str(r.get("outcome", "")),
                        sample_unix=float(r.get("sample_unix", 0.0) or 0.0),
                    ))
                    n += 1
                except (ValueError, TypeError):
                    continue
        except Exception:  # noqa: BLE001
            return n
        return n

    def mae(self, provider: str, route: str) -> Optional[float]:
        try:
            with self._lock:
                st = self._states.get(self._key(provider, route))
                return st.mae if st is not None else None
        except Exception:  # noqa: BLE001
            return None

    def sample_n(self, provider: str, route: str) -> int:
        try:
            with self._lock:
                st = self._states.get(self._key(provider, route))
                return st.n if st is not None else 0
        except Exception:  # noqa: BLE001
            return 0


__all__ = [
    "SCHEMA_VERSION",
    "PROVIDER_LATENCY_SCHEMA_VERSION",
    "TtftObserver",
    "TtftSample",
    "TtftStats",
    "ProviderLatencySample",
    "TtftForecaster",
    "ForecastResult",
    "provider_latency_forecast_enabled",
    "tracking_enabled",
    "ttft_demotion_enabled",
]
