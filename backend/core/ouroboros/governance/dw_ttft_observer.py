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


def _zeroshot_ttl_s() -> float:
    """TTL for a zero-shot (1-strike) timeout ban before the model decays back
    into a probing state. Env ``JARVIS_TTFT_ZEROSHOT_TTL_S``; default 8h. Clamped
    to [5min, 24h] so a transient API-instability window self-forgives without a
    typo permanently crippling the fleet OR thrashing on a still-broken model.
    NEVER raises."""
    raw = (os.environ.get("JARVIS_TTFT_ZEROSHOT_TTL_S", "") or "").strip()
    try:
        v = float(raw) if raw else 28800.0  # 8 hours
    except (TypeError, ValueError):
        v = 28800.0
    return max(300.0, min(v, 24 * 3600.0))


def zeroshot_timeout_quarantine_enabled() -> bool:
    """Master for the zero-shot timeout quarantine. Default TRUE — failure-path-
    only (only acts on an explicit TimeoutError). =0 reverts to pure σ-based
    cold-storage. NEVER raises."""
    return (os.environ.get("JARVIS_TTFT_ZEROSHOT_ENABLED", "true") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


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

    EWMA recency factor for the robust location-scale tracker (the
    single smoothing knob). Bounded ``(0, 1]`` — closer to 1 reacts
    faster to a congesting queue, closer to 0 is steadier. It is NOT
    a model coefficient: the baseline (median) and scale (log-MAD)
    are tracked from data, never set. (Name kept for the committed
    env-var seam; the math is no longer a "forecast" — see E1.)"""
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


def _mad_consistency_const() -> float:
    """``JARVIS_PROVIDER_LATENCY_MAD_CONSISTENCY`` (default 1.4826).

    The textbook constant that rescales a Median-Absolute-Deviation
    to a standard-deviation-equivalent under normality (1/Φ⁻¹(0.75)
    = 1.4826). It is a *principled statistical constant*, NOT a
    hand-tuned magic number — exactly like the Huber 1.345 we used
    earlier. Env-overridable; bounded ``[0.5, 5.0]``."""
    try:
        c = float(
            os.environ.get(
                "JARVIS_PROVIDER_LATENCY_MAD_CONSISTENCY", "1.4826",
            ).strip()
        )
    except (ValueError, TypeError):
        return 1.4826
    if not (0.5 <= c <= 5.0):
        return 1.4826
    return c


def _forecast_k() -> float:
    """``JARVIS_PROVIDER_LATENCY_FORECAST_K`` (default 3.0).

    Envelope width in robust-σ units: ``ceiling = exp(log-median +
    k · MAD_const · log-MAD)``. σ is MEASURED dispersion — k only
    chooses how many robust-σ of head-room the dynamic ceiling
    reserves against a congested queue. Bounded ``[0.5, 10.0]``."""
    try:
        k = float(
            os.environ.get(
                "JARVIS_PROVIDER_LATENCY_FORECAST_K", "3.0",
            ).strip()
        )
    except (ValueError, TypeError):
        return 3.0
    if not (0.5 <= k <= 10.0):
        return 3.0
    return k


# PHYSICAL ceiling on any exponentiated log value. A provider's
# time-to-FIRST-token cannot exceed ~1 hour in any real regime
# (worst observed: 73 s). exp(15.0) ≈ 3.27e6 ms ≈ 54 min — bounds
# a transient instability to "saturated but sane", never 1e13.
_MAX_LOG_EXPONENT = 15.0
_LOG_FLOOR_MS = 1.0


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
        # Zero-Shot Decay Matrix (2026-06-20): model_id → ban_unix for models
        # that hit the explicit generation TimeoutError wall. A single 180s
        # timeout is UNAMBIGUOUS evidence the model is unusable NOW — no σ window
        # needed (the n>=3 stddev test would let it taint 2 more soaks first). The
        # ban is NOT permanent: is_cold_storage decays it after a TTL so a
        # transient API-instability window self-forgives → model re-enters probing.
        self._zero_shot_bans: Dict[str, float] = {}
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
            # Zero-Shot bans — additive field; absent in pre-2026-06-20 files
            # (loads as empty, no schema bump → old state still loads clean).
            bans_raw = payload.get("zero_shot_bans", {})
            if isinstance(bans_raw, Mapping):
                for mid, ts in bans_raw.items():
                    try:
                        if isinstance(mid, str):
                            self._zero_shot_bans[mid] = float(ts)
                    except (ValueError, TypeError):
                        continue

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
                # Zero-Shot Decay Matrix — persisted so a 1-strike timeout ban
                # survives the subprocess fork boundary (immortal, like the
                # entitlement bans) yet still decays by wall-clock TTL on read.
                "zero_shot_bans": dict(self._zero_shot_bans),
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

    def record_timeout(self, model_id: str, op_id: str = "") -> None:
        """Zero-Shot quarantine (2026-06-20): flag ``model_id`` as cold-storage
        IMMEDIATELY on an explicit generation TimeoutError — bypassing the n>=3 σ
        window (a 180s timeout is unambiguous; waiting for a variance window lets
        the model taint two more soaks). The ban carries a wall-clock timestamp so
        is_cold_storage can DECAY it after the TTL → the model re-enters probing
        when the upstream latency resolves. NEVER raises."""
        if not model_id or not model_id.strip():
            return
        if not zeroshot_timeout_quarantine_enabled():
            return
        self._ensure_loaded()
        with self._lock:
            self._zero_shot_bans[model_id] = time.time()
            logger.warning(
                "[TtftObserver] ZERO-SHOT timeout quarantine: model=%s op=%s "
                "(TTL=%.0fs) — bypassing σ window", model_id, op_id, _zeroshot_ttl_s(),
            )
            self._maybe_autosave()

    def _zero_shot_active(self, model_id: str) -> bool:
        """True iff a non-expired zero-shot ban exists for ``model_id``. Expired
        bans are decayed (removed) here so the model re-enters probing. Caller
        holds ``self._lock``. NEVER raises."""
        ts = self._zero_shot_bans.get(model_id)
        if ts is None:
            return False
        if (time.time() - float(ts)) >= _zeroshot_ttl_s():
            # TTL elapsed — autonomic forgiveness: decay the ban, re-probe.
            self._zero_shot_bans.pop(model_id, None)
            logger.info(
                "[TtftObserver] zero-shot ban DECAYED (TTL elapsed) — "
                "model=%s re-enters probing", model_id,
            )
            return False
        return True

    def clear(self, model_id: str) -> None:
        """Drop all samples for ``model_id``. Used by sentinel on
        catalog refresh + by operator-driven reset paths. NEVER raises."""
        if not model_id or not model_id.strip():
            return
        self._ensure_loaded()
        with self._lock:
            removed = False
            if model_id in self._samples:
                del self._samples[model_id]
                removed = True
            if model_id in self._zero_shot_bans:
                self._zero_shot_bans.pop(model_id, None)
                removed = True
            if removed:
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
        # Zero-Shot bypass (with TTL decay): an explicit timeout quarantine fires
        # immediately, no σ window required, and self-forgives after the TTL.
        self._ensure_loaded()
        with self._lock:
            if self._zero_shot_active(model_id):
                return True
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
class EnvelopeResult:
    """One shadow ENVELOPE observation. Frozen.

    NOT a forecast — there is nothing to forecast: r(tokens,TTFT)
    ≈0.11, TTFT is bimodal & queue-driven, not token-driven (the
    falsification that retired the regression). We maintain a
    data-driven CEILING:

      * ``baseline_ms``  — EWMA-median TTFT (token-independent,
        hyper-stable: a 73 s spike barely moves it);
      * ``ceiling_ms``   — exp(log-median + k·MAD_const·log-MAD):
        the dynamic timeout the system WOULD adopt; inflates when
        the queue congests, deflates when healthy;
      * ``enveloped``    — did that ceiling cover this actual TTFT;
      * ``abs_dev_ms``   — |baseline − actual| (informational only;
        we are NOT scoring a prediction).

    ``baseline_ms``/``ceiling_ms`` are ``None`` below the
    minimum-samples floor (a robust dispersion is undefined for
    n<3 — the statistical definition, not a tuned gate).
    ``input_tokens`` is retained for observability ONLY; the model
    does not consume it."""
    provider: str
    route: str
    input_tokens: int
    baseline_ms: Optional[float]
    actual_ms: int
    abs_dev_ms: Optional[float]
    ceiling_ms: Optional[float]
    band_ms: Optional[float]
    enveloped: Optional[bool]
    n: int


# Back-compat alias: external composition seams may still import the
# old name. The illusion is gone from the SEMANTICS + fields; the
# symbol alias only avoids gratuitous import churn.
ForecastResult = EnvelopeResult


class _RobustState:
    """Streaming **robust location-scale** tracker for ONE provider
    key — NO slope, NO token term, NO OLS. (E1: the token-slope was
    falsified, r≈0.11.)

    Location = EWMA-**median** of ``ln(ttft_ms)`` via the
    Robbins-Monro 0.5-quantile recursion::

        lm ← lm + α · step · sign(ly − lm)

    A single 73 s spike moves ``lm`` by at most ``α·step`` —
    structurally bounded, NEVER proportional (this is why it cannot
    diverge the way the OLS slope did).

    Scale = EWMA of ``|ly − lm|`` — a robust **log-MAD** dispersion;
    rescaled by the textbook 1.4826 MAD→σ consistency constant only
    when read out.

    ``step`` self-adapts to ``max(scale, ε)`` so the median tracks
    at the data's own log-scale with no hardcoded step. Everything
    is derived from observed data; α is the only knob."""

    __slots__ = ("lm", "scale", "n")

    _EPS = 1e-6

    def __init__(self) -> None:
        self.lm = 0.0       # EWMA log-median (location)
        self.scale = 0.0    # EWMA |ly - lm|  (robust log dispersion)
        self.n = 0

    def update(self, y: float, alpha: float) -> None:
        """Fold ONE ttft observation (prequential — AFTER it has
        been scored against the standing ceiling)."""
        import math
        ly = math.log(max(_LOG_FLOOR_MS, float(y)))
        if self.n == 0:
            self.lm = ly
            self.scale = 0.0
            self.n = 1
            return
        dev = abs(ly - self.lm)
        # Scale first (so the step reflects current dispersion).
        self.scale += alpha * (dev - self.scale)
        step = self.scale if self.scale > self._EPS else self._EPS
        # Robbins-Monro median: bounded-influence location update.
        if ly > self.lm:
            self.lm += alpha * step
        elif ly < self.lm:
            self.lm -= alpha * step
        self.n += 1

    def _estimable(self) -> bool:
        # n>=3 is the statistical floor for a non-degenerate robust
        # dispersion (definition, not a tuned gate).
        return self.n >= _MIN_N_FOR_NONDEGENERATE_VARIANCE

    @staticmethod
    def _exp(v: float) -> float:
        import math
        return math.exp(min(v, _MAX_LOG_EXPONENT))

    def baseline(self) -> Optional[float]:
        """Token-independent baseline TTFT (ms) = exp(log-median).
        ``None`` until estimable. Structurally bounded."""
        if not self._estimable():
            return None
        return self._exp(self.lm)

    def envelope(
        self, k: float, mad_c: float,
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """``(baseline_ms, band_ms, ceiling_ms)``. The ceiling is
        the MULTIPLICATIVE robust band ``exp(log-median + k·c·
        log-MAD)`` — correct for a heavy-tailed/log-normal queue
        process: it inflates upward to swallow congestion spikes
        and deflates as they clear. Enforces nothing."""
        if not self._estimable():
            return (None, None, None)
        base = self._exp(self.lm)
        ceil = self._exp(self.lm + k * mad_c * self.scale)
        return (base, ceil - base, ceil)


class ProviderLatencyEnvelope:
    """Robust, token-INDEPENDENT latency-envelope tracker — SHADOW
    ONLY. Maintains a data-driven CEILING, it does NOT forecast
    (r(tokens,TTFT)≈0.11 falsified the regression).

    Composes the existing latency stream: folds each observed
    ``ProviderLatencySample`` into a tiny per-PROVIDER
    :class:`_RobustState` (route + input_tokens excluded from the
    key — Fix A unified pool). Public surface:

      * ``baseline(provider, route)`` → EWMA-median TTFT ms (or
        ``None`` before estimable). Token-independent.
      * ``envelope(provider, route, k)`` → ``(baseline, band,
        ceiling)``; ceiling is the dynamic timeout the system WOULD
        adopt — it INFLATES under queue congestion, DEFLATES when
        healthy.
      * ``observe(sample)`` → prequential: score the standing
        ceiling against the just-arrived actual, THEN fold it in.
      * ``warm_start_from_jsonl(path)`` → replay the durable
        Slice-0 dataset so the envelope is not cold on boot.

    STRICT SHADOW: only computes + the caller only logs. Returns no
    timeout, mutates no client, triggers no shedding (Slice 2/3).
    NEVER raises out of any public method."""

    def __init__(self) -> None:
        self._states: Dict[str, _RobustState] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(provider: str, route: str) -> str:
        # Fix A — Unified Statistical Keying. Queue/load latency
        # depends on the PROVIDER endpoint, NOT JARVIS's internal
        # route and (falsified) NOT on input_tokens. ``route`` kept
        # in the signature for callers/observability but DELIBERATELY
        # excluded from the key — all of one provider's traffic
        # pools into one robust estimator.
        return (provider or "").strip()

    def baseline(
        self, provider: str, route: str,
    ) -> Optional[float]:
        """Token-independent EWMA-median baseline TTFT (ms), or
        ``None`` until estimable. NEVER raises."""
        try:
            with self._lock:
                st = self._states.get(self._key(provider, route))
                return st.baseline() if st is not None else None
        except Exception:  # noqa: BLE001 — never raises
            return None

    # Back-compat shim: external seams may still call .forecast(...).
    # It is NOT a forecast — it returns the token-independent
    # baseline; the input_tokens arg is accepted and IGNORED.
    def forecast(
        self, provider: str, route: str, input_tokens: int = 0,
    ) -> Optional[float]:
        return self.baseline(provider, route)

    def envelope(
        self, provider: str, route: str,
        k: Optional[float] = None,
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """``(baseline_ms, band_ms, ceiling_ms)`` for the provider's
        unified pool. The value a future Slice-2 dynamic HTTP
        timeout WOULD adopt. Enforces nothing. NEVER raises."""
        try:
            kk = _forecast_k() if k is None else float(k)
            mad_c = _mad_consistency_const()
            with self._lock:
                st = self._states.get(self._key(provider, route))
                if st is None:
                    return (None, None, None)
                return st.envelope(kk, mad_c)
        except Exception:  # noqa: BLE001 — never raises
            return (None, None, None)

    def observe(self, sample: "ProviderLatencySample") -> EnvelopeResult:
        """Prequential step. Read the STANDING ceiling, score the
        just-arrived actual against it, THEN fold the sample. Only
        meaningful for streaming successes — degenerate rows
        (timeout ttft=-1, non-success) are passed through as a
        transparent skip and NOT folded (they would bias the
        robust scale). ``input_tokens`` is ignored by the model
        (token-independent) — retained only in the result for
        observability. NEVER raises."""
        try:
            if not isinstance(sample, ProviderLatencySample):
                return EnvelopeResult(
                    "", "", 0, None, 0, None, None, None, None, 0,
                )
            prov = str(sample.provider or "")
            route = str(sample.route or "")
            xtok = int(max(0, int(sample.input_tokens)))
            y = int(sample.ttft_ms)
            key = self._key(prov, route)
            alpha = _forecast_alpha()
            k = _forecast_k()
            mad_c = _mad_consistency_const()
            with self._lock:
                st = self._states.get(key)
                if st is None:
                    st = _RobustState()
                    self._states[key] = st
                if y < 0 or sample.outcome != "success":
                    base0, band0, ceil0 = st.envelope(k, mad_c)
                    return EnvelopeResult(
                        prov, route, xtok, base0, max(0, y),
                        None, ceil0, band0, None, st.n,
                    )
                # Prequential: ceiling/baseline from the STANDING
                # state, score, THEN fold (no peeking).
                base, band, ceil = st.envelope(k, mad_c)
                enveloped = (
                    bool(ceil >= y) if ceil is not None else None
                )
                abs_dev = (
                    abs(base - y) if base is not None else None
                )
                st.update(float(y), alpha)
                return EnvelopeResult(
                    prov, route, xtok, base, y, abs_dev,
                    ceil, band, enveloped, st.n,
                )
        except Exception:  # noqa: BLE001 — never raises
            return EnvelopeResult(
                "", "", 0, None, 0, None, None, None, None, 0,
            )

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

    def sample_n(self, provider: str, route: str) -> int:
        try:
            with self._lock:
                st = self._states.get(self._key(provider, route))
                return st.n if st is not None else 0
        except Exception:  # noqa: BLE001
            return 0


# Back-compat alias for external composition seams (the singleton
# accessor + tests imported the old name). The "forecast" illusion
# is gone from the SEMANTICS, fields and math; the symbol alias
# only avoids gratuitous import churn across the codebase.
TtftForecaster = ProviderLatencyEnvelope


__all__ = [
    "SCHEMA_VERSION",
    "PROVIDER_LATENCY_SCHEMA_VERSION",
    "TtftObserver",
    "TtftSample",
    "TtftStats",
    "ProviderLatencySample",
    "ProviderLatencyEnvelope",
    "TtftForecaster",
    "EnvelopeResult",
    "ForecastResult",
    "provider_latency_forecast_enabled",
    "tracking_enabled",
    "ttft_demotion_enabled",
]
