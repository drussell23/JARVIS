"""Phase 12.2 Slice A — full-jitter backoff regression spine.

Pins:
  §1 Master flag default off + truthy/falsy parsing
  §2 attempt=0 → [0, base_s]
  §3 attempt=N → [0, min(cap, base * 2^N)]
  §4 Cap enforced (large attempt clamped to cap_s)
  §5 Determinism with seeded rng (same seed → same sequence)
  §6 Non-determinism without rng (different calls → different values)
  §7 Negative attempt → clamped to 0
  §8 Overflow on huge attempt → clamped to cap_s, not OverflowError
  §9 Non-positive base_s / cap_s → coerced to env defaults
  §10 Env override of base_s / cap_s
  §11 NEVER raises on garbage input
  §12 Statistical: distribution is uniform across [0, upper]
"""
from __future__ import annotations

import random
from typing import Any  # noqa: F401

import pytest

from backend.core.ouroboros.governance.full_jitter import (
    full_jitter_backoff_s,
    full_jitter_enabled,
)


# ---------------------------------------------------------------------------
# §1 — Master flag
# ---------------------------------------------------------------------------


def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_TOPOLOGY_FULL_JITTER_ENABLED", raising=False)
    assert full_jitter_enabled() is False


def test_flag_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_TOPOLOGY_FULL_JITTER_ENABLED", v)
        assert full_jitter_enabled() is True


def test_flag_falsy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("JARVIS_TOPOLOGY_FULL_JITTER_ENABLED", v)
        assert full_jitter_enabled() is False


# ---------------------------------------------------------------------------
# §2 — attempt=0 → [0, base_s]
# ---------------------------------------------------------------------------


def test_attempt_zero_within_base() -> None:
    rng = random.Random(42)
    delays = [
        full_jitter_backoff_s(0, base_s=10.0, cap_s=300.0, rng=rng)
        for _ in range(50)
    ]
    for d in delays:
        assert 0.0 <= d <= 10.0, f"delay {d} out of [0, 10] for attempt=0"


# ---------------------------------------------------------------------------
# §3 — attempt=N → [0, min(cap, base*2^N)]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attempt,expected_upper", [
    (0, 10.0),    # 10 * 2^0 = 10
    (1, 20.0),    # 10 * 2^1 = 20
    (2, 40.0),    # 10 * 2^2 = 40
    (3, 80.0),    # 10 * 2^3 = 80
    (4, 160.0),   # 10 * 2^4 = 160
    (5, 300.0),   # 10 * 2^5 = 320 > cap=300 → clamped
    (10, 300.0),  # cap dominates
])
def test_upper_bound_per_attempt(attempt: int, expected_upper: float) -> None:
    rng = random.Random(42)
    delays = [
        full_jitter_backoff_s(
            attempt, base_s=10.0, cap_s=300.0, rng=rng,
        )
        for _ in range(100)
    ]
    for d in delays:
        assert 0.0 <= d <= expected_upper, (
            f"delay {d} exceeds upper {expected_upper} at attempt={attempt}"
        )
    # The MAX observed should be near the upper bound (high enough N
    # that uniform sampling will land near the top).
    assert max(delays) > expected_upper * 0.7, (
        f"max delay {max(delays)} suspiciously low for upper {expected_upper}"
    )


# ---------------------------------------------------------------------------
# §4 — Cap enforced (large attempt clamped)
# ---------------------------------------------------------------------------


def test_huge_attempt_clamped_to_cap() -> None:
    """attempt=1000 would overflow 2^N → must clamp to cap_s, not raise."""
    rng = random.Random(0)
    for _ in range(20):
        d = full_jitter_backoff_s(
            1000, base_s=10.0, cap_s=300.0, rng=rng,
        )
        assert 0.0 <= d <= 300.0


def test_attempt_exactly_at_cap_boundary() -> None:
    """Find the attempt where base*2^N == cap. attempt=5 with base=10
    cap=320 has scaled=320; cap=300 means upper=300 always."""
    rng = random.Random(0)
    for _ in range(20):
        d = full_jitter_backoff_s(5, base_s=10.0, cap_s=320.0, rng=rng)
        assert 0.0 <= d <= 320.0


# ---------------------------------------------------------------------------
# §5 — Determinism with seeded rng
# ---------------------------------------------------------------------------


def test_seeded_rng_produces_deterministic_sequence() -> None:
    """Same seed → same delay sequence. Pin source-level so a
    refactor that changes the rng calling convention fails."""
    rng1 = random.Random(12345)
    rng2 = random.Random(12345)
    seq1 = [
        full_jitter_backoff_s(i, base_s=5.0, cap_s=100.0, rng=rng1)
        for i in range(10)
    ]
    seq2 = [
        full_jitter_backoff_s(i, base_s=5.0, cap_s=100.0, rng=rng2)
        for i in range(10)
    ]
    assert seq1 == seq2, "seeded rng must produce deterministic delays"


def test_different_seeds_produce_different_sequences() -> None:
    rng1 = random.Random(1)
    rng2 = random.Random(2)
    seq1 = [
        full_jitter_backoff_s(0, base_s=10.0, cap_s=100.0, rng=rng1)
        for _ in range(5)
    ]
    seq2 = [
        full_jitter_backoff_s(0, base_s=10.0, cap_s=100.0, rng=rng2)
        for _ in range(5)
    ]
    assert seq1 != seq2


# ---------------------------------------------------------------------------
# §6 — Non-determinism without rng
# ---------------------------------------------------------------------------


def test_no_rng_produces_varying_values() -> None:
    """Without seeded rng, repeated calls produce different delays
    (statistically — extremely unlikely all 50 are equal)."""
    delays = [
        full_jitter_backoff_s(2, base_s=10.0, cap_s=300.0)
        for _ in range(50)
    ]
    distinct = set(delays)
    assert len(distinct) > 30, (
        f"expected variation; got only {len(distinct)} distinct values"
    )


# ---------------------------------------------------------------------------
# §7 — Negative attempt → clamped to 0
# ---------------------------------------------------------------------------


def test_negative_attempt_clamps_to_zero() -> None:
    rng = random.Random(0)
    for _ in range(20):
        d = full_jitter_backoff_s(-5, base_s=10.0, cap_s=300.0, rng=rng)
        # attempt=-5 should behave as attempt=0 → upper=10
        assert 0.0 <= d <= 10.0


# ---------------------------------------------------------------------------
# §8 — Overflow protection
# ---------------------------------------------------------------------------


def test_extreme_attempt_no_overflow_error() -> None:
    """attempt=10000 in 2^attempt would overflow; we catch it."""
    d = full_jitter_backoff_s(10000, base_s=10.0, cap_s=300.0)
    assert 0.0 <= d <= 300.0


# ---------------------------------------------------------------------------
# §9 — Non-positive base/cap coerced
# ---------------------------------------------------------------------------


def test_zero_base_s_coerces_to_env_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_BACKOFF_BASE_S", "5.0")
    rng = random.Random(0)
    d = full_jitter_backoff_s(0, base_s=0, cap_s=100.0, rng=rng)
    # base coerced to 5.0 → upper=5.0 for attempt=0
    assert 0.0 <= d <= 5.0


def test_zero_cap_s_coerces_to_env_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_BACKOFF_CAP_S", "50.0")
    rng = random.Random(0)
    d = full_jitter_backoff_s(10, base_s=10.0, cap_s=0, rng=rng)
    # cap coerced to 50 → upper=50
    assert 0.0 <= d <= 50.0


def test_negative_base_s_coerces() -> None:
    rng = random.Random(0)
    d = full_jitter_backoff_s(0, base_s=-5.0, cap_s=100.0, rng=rng)
    # negative coerces to default → 10.0
    assert 0.0 <= d <= 10.0


# ---------------------------------------------------------------------------
# §10 — Env override defaults
# ---------------------------------------------------------------------------


def test_env_base_s_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_BACKOFF_BASE_S", "20.0")
    rng = random.Random(0)
    delays = [
        full_jitter_backoff_s(0, rng=rng)
        for _ in range(50)
    ]
    # Should be in [0, 20] now
    for d in delays:
        assert 0.0 <= d <= 20.0


def test_env_cap_s_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_BACKOFF_BASE_S", "100.0")
    monkeypatch.setenv("JARVIS_TOPOLOGY_BACKOFF_CAP_S", "150.0")
    rng = random.Random(0)
    # attempt=10 → 100*1024=102400, capped at 150
    for _ in range(20):
        d = full_jitter_backoff_s(10, rng=rng)
        assert 0.0 <= d <= 150.0


def test_invalid_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage env values don't crash — coerce to default."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_BACKOFF_BASE_S", "not-a-number")
    rng = random.Random(0)
    d = full_jitter_backoff_s(0, rng=rng)
    # Default base=10 → upper=10
    assert 0.0 <= d <= 10.0


# ---------------------------------------------------------------------------
# §11 — NEVER raises on garbage
# ---------------------------------------------------------------------------


def test_never_raises_on_bool_attempt() -> None:
    """Python booleans are int subtypes — make sure True/False don't
    accidentally produce different output than 1/0."""
    d_true = full_jitter_backoff_s(True, base_s=10.0, cap_s=100.0,
                                    rng=random.Random(0))
    d_false = full_jitter_backoff_s(False, base_s=10.0, cap_s=100.0,
                                     rng=random.Random(0))
    # Both should be in [0, 10] (clamped to attempt=0 by isinstance check)
    assert 0.0 <= d_true <= 10.0
    assert 0.0 <= d_false <= 10.0


def test_never_raises_on_float_attempt() -> None:
    """Python's int() coerces 2.7 → 2."""
    d = full_jitter_backoff_s(2.7, base_s=10.0, cap_s=300.0,  # type: ignore[arg-type]
                               rng=random.Random(0))
    # int(2.7)=2 → upper = 10*4 = 40
    assert 0.0 <= d <= 40.0


# ---------------------------------------------------------------------------
# §12 — Statistical: distribution is uniform
# ---------------------------------------------------------------------------


def test_distribution_is_approximately_uniform() -> None:
    """Run 10000 calls at attempt=2 (upper=40), check that the
    distribution histogram is roughly flat (no exponential bias)."""
    rng = random.Random(7)
    delays = [
        full_jitter_backoff_s(2, base_s=10.0, cap_s=300.0, rng=rng)
        for _ in range(10000)
    ]
    # Bucket into 4 quartiles of [0, 40]
    bins = [0, 0, 0, 0]
    for d in delays:
        idx = min(3, int(d / 10))
        bins[idx] += 1
    # Each bucket should have ~2500 samples (10000/4) — within 15%
    for count in bins:
        assert 2125 < count < 2875, (
            f"non-uniform distribution: bins={bins} (expected ~2500 each)"
        )


def test_mean_is_approximately_half_of_upper() -> None:
    """For a uniform distribution on [0, U], expected mean = U/2."""
    rng = random.Random(7)
    delays = [
        full_jitter_backoff_s(0, base_s=10.0, cap_s=300.0, rng=rng)
        for _ in range(5000)
    ]
    mean = sum(delays) / len(delays)
    # Expected mean = 5.0; allow 5% tolerance (Central Limit Theorem
    # gives stddev of mean ~= 10/sqrt(12*5000) ≈ 0.04)
    assert 4.7 < mean < 5.3, f"mean {mean:.3f} not near 5.0"


# ---------------------------------------------------------------------------
# §13 — Source-level pin: the math
# ---------------------------------------------------------------------------


def test_source_uses_uniform_not_exponential() -> None:
    """Pin the literal random.uniform(0, upper) call — a refactor
    that switches to .gauss() or another distribution would silently
    break the desync property."""
    import inspect
    src = inspect.getsource(full_jitter_backoff_s)
    assert ".uniform(0.0, upper)" in src or ".uniform(0, upper)" in src


def test_source_uses_two_to_attempt_power() -> None:
    """The exponential expansion formula must be 2^attempt — pin it
    so a refactor doesn't silently change the backoff curve."""
    import inspect
    src = inspect.getsource(full_jitter_backoff_s)
    assert "2 ** a" in src or "2**a" in src
