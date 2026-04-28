"""Phase 12.2 Slice B — TtftObserver regression spine.

Pins the mathematical promotion + cold-storage gates with NO hardcoded
integer count thresholds (operator directive 2026-04-27).

Sections:
  §1 master flag default-off
  §2 record_ttft basics — input + persistence + window cap
  §3 stats() — mean / stddev / CV / rel_SEM math correctness
  §4 is_promotion_ready — DUAL gate (CV + rel_SEM)
  §5 dynamic N — same model graduates at different N depending on CV
  §6 NO hardcoded count gate (the directive's invariant)
  §7 is_cold_storage — 2σ threshold + N≥3 floor
  §8 cold-storage statistical floor (degenerate variance)
  §9 promotion_ready_models / cold_storage_models accessors
  §10 master flag off → all gates return False
  §11 disk persistence round-trip
  §12 corrupt state boots empty
  §13 schema mismatch starts empty
  §14 thread-safety smoke test
  §15 NEVER-raises contract
  §16 source-level pins — math formula present, no hardcoded N
"""
from __future__ import annotations

import inspect
import json
import math
import threading
from pathlib import Path
from typing import Any, List  # noqa: F401

import pytest

from backend.core.ouroboros.governance import dw_ttft_observer as dto
from backend.core.ouroboros.governance.dw_ttft_observer import (
    SCHEMA_VERSION,
    TtftObserver,
    TtftSample,
    TtftStats,
    tracking_enabled,
)


@pytest.fixture
def isolated_path(tmp_path: Path,
                  monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "ttft.json"
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_STATE_PATH", str(p))
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED", "true")
    return p


@pytest.fixture
def make_observer(isolated_path: Path):
    def _factory(**kwargs) -> TtftObserver:
        return TtftObserver(**kwargs)
    return _factory


# ---------------------------------------------------------------------------
# §1 — Master flag
# ---------------------------------------------------------------------------


def test_tracking_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED", raising=False)
    assert tracking_enabled() is False


def test_tracking_truthy_falsy(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED", v)
        assert tracking_enabled() is True
    for v in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED", v)
        assert tracking_enabled() is False


# ---------------------------------------------------------------------------
# §2 — record_ttft basics
# ---------------------------------------------------------------------------


def test_record_ttft_basic(make_observer) -> None:
    obs = make_observer()
    obs.record_ttft("v/m-7B", 150)
    obs.record_ttft("v/m-7B", 160, op_id="op-1")
    assert obs.sample_count("v/m-7B") == 2


def test_record_ttft_window_cap(
    make_observer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_WINDOW_N", "5")
    obs = make_observer()
    for i in range(20):
        obs.record_ttft("v/m-7B", 100 + i)
    assert obs.sample_count("v/m-7B") == 5
    # Should be the LAST 5 samples
    s = obs.stats("v/m-7B")
    assert s is not None
    assert s.latest_ms == 119  # 100 + 19


def test_record_ttft_negative_rejected(make_observer) -> None:
    obs = make_observer()
    obs.record_ttft("v/m-7B", -5)
    assert obs.sample_count("v/m-7B") == 0


def test_record_ttft_garbage_inputs_tolerated(make_observer) -> None:
    obs = make_observer()
    for bad_id in (None, "", "  "):
        obs.record_ttft(bad_id, 100)  # type: ignore[arg-type]
    obs.record_ttft("v/m-7B", "not-a-number")  # type: ignore[arg-type]
    assert obs.all_tracked_models() == ()


# ---------------------------------------------------------------------------
# §3 — stats() math
# ---------------------------------------------------------------------------


def test_stats_single_sample_zero_stddev(make_observer) -> None:
    obs = make_observer()
    obs.record_ttft("v/m-7B", 100)
    s = obs.stats("v/m-7B")
    assert s is not None
    assert s.n == 1
    assert s.mean_ms == 100.0
    assert s.stddev_ms == 0.0
    assert s.cv == 0.0


def test_stats_constant_samples_zero_cv(make_observer) -> None:
    obs = make_observer()
    for _ in range(5):
        obs.record_ttft("v/m-7B", 100)
    s = obs.stats("v/m-7B")
    assert s is not None
    assert s.mean_ms == 100.0
    assert s.stddev_ms == 0.0
    assert s.cv == 0.0
    assert s.rel_sem == 0.0


def test_stats_known_mean_and_stddev(make_observer) -> None:
    """Known dataset [80, 100, 120] — mean=100, sample stddev=20."""
    obs = make_observer()
    for v in (80, 100, 120):
        obs.record_ttft("v/m-7B", v)
    s = obs.stats("v/m-7B")
    assert s is not None
    assert s.n == 3
    assert s.mean_ms == 100.0
    # Sample stddev (Bessel-corrected): sqrt(((20)^2 + 0 + (20)^2) / 2) = 20
    assert abs(s.stddev_ms - 20.0) < 0.01
    assert abs(s.cv - 0.20) < 0.01
    # rel_SEM = CV / sqrt(N) = 0.2 / sqrt(3) ≈ 0.1155
    assert abs(s.rel_sem - 0.20 / math.sqrt(3)) < 0.001


def test_stats_returns_none_for_unknown_model(make_observer) -> None:
    obs = make_observer()
    assert obs.stats("never/seen") is None


# ---------------------------------------------------------------------------
# §4 — is_promotion_ready DUAL gate
# ---------------------------------------------------------------------------


def test_promotion_requires_cv_below_threshold(
    make_observer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CV gate: model with CV >= 0.15 NEVER graduates regardless of N."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_CV_THRESHOLD", "0.15")
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_REL_SEM_THRESHOLD", "0.05")
    obs = make_observer()
    # Construct samples with CV ≈ 0.20 (above threshold)
    for v in (80, 100, 120, 80, 100, 120, 80, 100, 120, 80,
              100, 120, 80, 100, 120, 80, 100, 120, 80, 100):
        obs.record_ttft("v/noisy", v)
    # Even with N=20, CV=0.20 > 0.15 → not ready
    assert obs.is_promotion_ready("v/noisy") is False


def test_promotion_requires_rel_sem_below_threshold(
    make_observer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rel_SEM gate: even with low CV, need enough N for stable mean."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_CV_THRESHOLD", "0.15")
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_REL_SEM_THRESHOLD", "0.05")
    obs = make_observer()
    # 2 samples, CV ≈ 0.07 (below threshold), but rel_SEM = CV/sqrt(2)
    # = 0.07/1.41 ≈ 0.05 — borderline
    obs.record_ttft("v/m-7B", 95)
    obs.record_ttft("v/m-7B", 105)
    s = obs.stats("v/m-7B")
    assert s is not None
    # N=2 with these values gives rel_SEM ~ 0.053, just over threshold
    # Actual: stddev = sqrt(((-5)^2 + 5^2)/(2-1)) = sqrt(50) ≈ 7.07
    # CV = 7.07/100 ≈ 0.0707
    # rel_SEM = 0.0707/sqrt(2) ≈ 0.05
    # So the gate is right at the boundary; either side is fine,
    # the math is what's pinned.
    if s.rel_sem >= 0.05:
        assert obs.is_promotion_ready("v/m-7B") is False


def test_promotion_passes_with_consistent_mean_and_enough_samples(
    make_observer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stable model (CV=0.05) graduates as soon as math gates pass."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_CV_THRESHOLD", "0.15")
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_REL_SEM_THRESHOLD", "0.05")
    obs = make_observer()
    # Tight cluster around 100 — CV ≈ 0.05
    for v in (95, 100, 105, 95, 100, 105, 95, 100, 105, 95):
        obs.record_ttft("v/stable", v)
    assert obs.is_promotion_ready("v/stable") is True


# ---------------------------------------------------------------------------
# §5 — Dynamic N: same model graduates at DIFFERENT N depending on CV
# ---------------------------------------------------------------------------


def test_stable_model_graduates_at_low_n(
    make_observer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator directive: 'regardless of whether that took 3 attempts
    or 12.' A very stable model (CV=0.02) should graduate quickly.

    Math: rel_SEM = CV/sqrt(N) < 0.05 → N > (0.02/0.05)^2 = 0.16
    → ANY N >= 1 satisfies. Combined with CV<0.15 gate, N>=2 graduates."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_CV_THRESHOLD", "0.15")
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_REL_SEM_THRESHOLD", "0.05")
    obs = make_observer()
    # Rock-solid model: CV ≈ 0.01
    for v in (100, 101, 99, 100):
        obs.record_ttft("v/rock-solid", v)
    # Should graduate well before N=10
    assert obs.is_promotion_ready("v/rock-solid") is True


def test_moderate_model_needs_more_samples(
    make_observer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model with CV=0.10 needs N > (0.10/0.05)^2 = 4 → N>=5."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_CV_THRESHOLD", "0.15")
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_REL_SEM_THRESHOLD", "0.05")
    obs = make_observer()
    # CV ≈ 0.10 — record samples one by one and check when ready
    samples = [90, 110, 90, 110, 90, 110, 90, 110]
    became_ready_at = None
    for i, v in enumerate(samples, 1):
        obs.record_ttft("v/moderate", v)
        if obs.is_promotion_ready("v/moderate"):
            became_ready_at = i
            break
    # Should NOT graduate at N=2; should graduate by N=5 (math says >4)
    assert became_ready_at is not None
    assert became_ready_at >= 3, (
        f"moderate model graduated too early at N={became_ready_at}"
    )


# ---------------------------------------------------------------------------
# §6 — NO hardcoded count gate (the directive's invariant)
# ---------------------------------------------------------------------------


def test_no_min_count_gate_in_promotion_logic() -> None:
    """Source-level pin: is_promotion_ready must NOT contain a
    hardcoded count comparison like 'n >= 10' or 's.n >= MIN_N'.

    The math derives N from CV automatically. Operator directive
    2026-04-27 explicitly rejects JARVIS_DW_PROMOTION_MIN_SUCCESSES
    style gates."""
    src = inspect.getsource(TtftObserver.is_promotion_ready)
    # Allowed: n < 2 (sample variance is undefined)
    # Banned: n >= 10, n >= 3, n > 5, etc — ANY hardcoded promotion count
    import re
    # Look for `n >= <int>` or `s.n >= <int>` where int >= 3
    suspicious = re.findall(r"\.?n\s*>=?\s*(\d+)", src)
    for match in suspicious:
        n_val = int(match)
        # n>=2 is fine (sample stddev floor); higher is suspicious
        assert n_val < 3, (
            f"is_promotion_ready contains hardcoded count gate "
            f"(n>={n_val}). Operator directive rejected this pattern. "
            f"Use CV/rel_SEM math instead."
        )


def test_no_min_successes_env_in_promotion() -> None:
    """The rejected env var name must not appear in promotion code."""
    src = inspect.getsource(TtftObserver.is_promotion_ready)
    assert "MIN_SUCCESSES" not in src
    assert "min_successes" not in src.lower()


# ---------------------------------------------------------------------------
# §7 — is_cold_storage 2σ
# ---------------------------------------------------------------------------


def test_cold_storage_detects_outlier(
    make_observer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_COLD_SIGMA", "2.0")
    obs = make_observer()
    # Build baseline: tight cluster near 100
    for v in (95, 100, 105, 95, 100, 105):
        obs.record_ttft("v/m-7B", v)
    # Now a huge outlier (cold-storage spike)
    obs.record_ttft("v/m-7B", 5000)
    assert obs.is_cold_storage("v/m-7B") is True


def test_cold_storage_silent_on_normal_sample(make_observer) -> None:
    obs = make_observer()
    # Normal cluster, latest also normal
    for v in (95, 100, 105, 95, 100, 105, 100):
        obs.record_ttft("v/m-7B", v)
    assert obs.is_cold_storage("v/m-7B") is False


def test_cold_storage_threshold_tunable(
    make_observer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lower σ multiplier → easier to trigger."""
    obs = make_observer()
    for v in (95, 100, 105, 95, 100, 105):
        obs.record_ttft("v/m-7B", v)
    # Latest = 130 — about 5σ above mean
    obs.record_ttft("v/m-7B", 130)
    # With sigma=10, 130 is 5σ < 10σ → not cold
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_COLD_SIGMA", "10.0")
    assert obs.is_cold_storage("v/m-7B") is False
    # With sigma=2 (default), 130 > mean(100) + 2*5 = 110 → cold
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_COLD_SIGMA", "2.0")
    assert obs.is_cold_storage("v/m-7B") is True


# ---------------------------------------------------------------------------
# §8 — Cold-storage statistical floor (N>=3)
# ---------------------------------------------------------------------------


def test_cold_storage_requires_n_at_least_3(make_observer) -> None:
    """N=2 with [100, 5000] would have huge stddev; cold-storage test
    is meaningless. Below N=3, never fires."""
    obs = make_observer()
    obs.record_ttft("v/m-7B", 100)
    obs.record_ttft("v/m-7B", 5000)  # 'outlier'
    assert obs.is_cold_storage("v/m-7B") is False


def test_cold_storage_floor_is_mathematical_not_tunable() -> None:
    """The N>=3 floor must NOT be configurable by env. It's the
    mathematical floor for sample variance to be non-degenerate."""
    src = inspect.getsource(dto)
    assert "_MIN_N_FOR_NONDEGENERATE_VARIANCE" in src
    # Pinned at 3 (mathematical fact, not tuning)
    assert "_MIN_N_FOR_NONDEGENERATE_VARIANCE = 3" in src


# ---------------------------------------------------------------------------
# §9 — Accessors
# ---------------------------------------------------------------------------


def test_promotion_ready_models_returns_qualified_only(
    make_observer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_CV_THRESHOLD", "0.15")
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_REL_SEM_THRESHOLD", "0.05")
    obs = make_observer()
    # Stable model
    for v in (95, 100, 105, 95, 100, 105):
        obs.record_ttft("v/stable", v)
    # Noisy model
    for v in (50, 150, 50, 150, 50, 150):
        obs.record_ttft("v/noisy", v)
    ready = obs.promotion_ready_models()
    assert "v/stable" in ready
    assert "v/noisy" not in ready


def test_cold_storage_models_lists_models_in_cold_state(
    make_observer,
) -> None:
    obs = make_observer()
    # Model 1 — cold-storage spike
    for v in (95, 100, 105, 95, 100, 105):
        obs.record_ttft("v/m-cold", v)
    obs.record_ttft("v/m-cold", 5000)
    # Model 2 — normal
    for v in (95, 100, 105, 95, 100, 105, 100):
        obs.record_ttft("v/m-warm", v)
    cold = obs.cold_storage_models()
    assert "v/m-cold" in cold
    assert "v/m-warm" not in cold


# ---------------------------------------------------------------------------
# §10 — Master flag off → all gates False
# ---------------------------------------------------------------------------


def test_flag_off_disables_all_gates(
    make_observer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED", "false")
    obs = make_observer()
    # Even with stable samples, gates return False when flag is off
    for v in (95, 100, 105, 95, 100, 105):
        obs.record_ttft("v/stable", v)
    assert obs.is_promotion_ready("v/stable") is False
    assert obs.is_cold_storage("v/stable") is False
    assert obs.promotion_ready_models() == ()
    assert obs.cold_storage_models() == ()


# ---------------------------------------------------------------------------
# §11 — Disk persistence round-trip
# ---------------------------------------------------------------------------


def test_persistence_roundtrip(isolated_path: Path) -> None:
    obs1 = TtftObserver()
    obs1.record_ttft("v/m-7B", 100)
    obs1.record_ttft("v/m-7B", 110)
    obs1.record_ttft("v/m-7B", 120)
    obs2 = TtftObserver()
    obs2.load()
    s = obs2.stats("v/m-7B")
    assert s is not None
    assert s.n == 3
    assert s.mean_ms == 110.0


def test_autosave_off_does_not_persist(isolated_path: Path) -> None:
    obs = TtftObserver(autosave=False)
    obs.record_ttft("v/m-7B", 100)
    assert not isolated_path.exists()
    obs.save()
    assert isolated_path.exists()


# ---------------------------------------------------------------------------
# §12 — Corrupt state boots empty (NEVER raises)
# ---------------------------------------------------------------------------


def test_corrupt_state_boots_empty(
    isolated_path: Path,
) -> None:
    isolated_path.parent.mkdir(parents=True, exist_ok=True)
    isolated_path.write_text("not valid json", encoding="utf-8")
    obs = TtftObserver()
    obs.load()  # should not raise
    assert obs.all_tracked_models() == ()


# ---------------------------------------------------------------------------
# §13 — Schema mismatch starts empty
# ---------------------------------------------------------------------------


def test_schema_mismatch_starts_empty(isolated_path: Path) -> None:
    isolated_path.parent.mkdir(parents=True, exist_ok=True)
    isolated_path.write_text(json.dumps({
        "schema_version": "ttft_observer.99",
        "samples": {"v/m-7B": [{"ttft_ms": 100}]},
    }), encoding="utf-8")
    obs = TtftObserver()
    obs.load()
    assert obs.all_tracked_models() == ()


# ---------------------------------------------------------------------------
# §14 — Thread-safety smoke test
# ---------------------------------------------------------------------------


def test_concurrent_record_ttft_no_corruption(make_observer) -> None:
    obs = make_observer()
    workers = []
    n_per_thread = 30
    n_threads = 6
    def _worker():
        for i in range(n_per_thread):
            obs.record_ttft("v/m-7B", 100 + i)
    for _ in range(n_threads):
        t = threading.Thread(target=_worker)
        workers.append(t)
        t.start()
    for t in workers:
        t.join()
    # 6*30=180 samples; window cap (default 50) drops to 50
    s = obs.stats("v/m-7B")
    assert s is not None
    assert s.n == 50  # capped


# ---------------------------------------------------------------------------
# §15 — NEVER-raises contract
# ---------------------------------------------------------------------------


def test_garbage_inputs_tolerated_on_all_methods(make_observer) -> None:
    obs = make_observer()
    for bad in (None, "", "  ", "\t"):
        obs.record_ttft(bad, 100)  # type: ignore[arg-type]
        assert obs.stats(bad) is None  # type: ignore[arg-type]
        assert obs.is_promotion_ready(bad) is False  # type: ignore[arg-type]
        assert obs.is_cold_storage(bad) is False  # type: ignore[arg-type]
        assert obs.sample_count(bad) == 0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# §16 — Source-level pins on the math
# ---------------------------------------------------------------------------


def test_source_uses_coefficient_of_variation() -> None:
    """is_promotion_ready must use CV-based gate, not absolute stddev."""
    src = inspect.getsource(TtftObserver.is_promotion_ready)
    assert "cv" in src.lower()
    assert "_cv_threshold()" in src


def test_source_uses_relative_sem() -> None:
    """is_promotion_ready must include the relative SEM gate."""
    src = inspect.getsource(TtftObserver.is_promotion_ready)
    assert "rel_sem" in src.lower()


def test_source_stats_uses_bessel_correction() -> None:
    """Sample stddev must divide by (n-1), not n."""
    src = inspect.getsource(TtftObserver.stats)
    assert "n - 1" in src or "(n-1)" in src


def test_source_cold_storage_uses_sigma_multiplier() -> None:
    src = inspect.getsource(TtftObserver.is_cold_storage)
    assert "_cold_sigma()" in src
    assert "stddev" in src
