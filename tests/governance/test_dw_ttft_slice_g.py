"""Phase 12.2 Slice G — Absolute Ceiling Gate + Failure Ignorance.

Closes the zero-order flaw discovered during the once-proof of
``bt-2026-04-28-201119``: a model returning uniform 30-second
timeouts has CV=0 and rel_SEM=0 (trivially "consistent") which
would pass the variance gates and falsely promote a functionally
dead model.

Two surfaces:

  1. ``TtftObserver.is_promotion_ready`` — adds an absolute mean_ms
     ceiling check BEFORE the variance math. Rejects models whose
     mean is above ``JARVIS_TOPOLOGY_TTFT_PROMOTION_CEILING_MS``
     (default 5000ms).

  2. ``HeavyProber.probe`` — failure ignorance. ConnectionTimeoutError
     and other transport failures are NOT recorded into the observer.
     The HeavyProbeResult still reports the ceiling for introspection,
     but no sample poisons the warmth dataset.

Pins:
  §1  promotion_ceiling_ms env reader — default 5000ms, case-tolerant
  §2  Ceiling gate fires on mean_ms == ceiling
  §3  Ceiling gate fires on mean_ms > ceiling
  §4  Ceiling gate uniform-timeout false positive REJECTED (the bug)
  §5  Ceiling gate composes with CV gate (both must pass)
  §6  Ceiling gate composes with rel_SEM gate (both must pass)
  §7  Ceiling gate respects env override (operator can lower)
  §8  Heavy probe failure ignorance — 500 response → no observer call
  §9  Heavy probe failure ignorance — transport error → no observer call
  §10 Heavy probe failure ignorance — empty stream → no observer call
  §11 Heavy probe success ignorance preserved — real chunk → observer recorded
  §12 Result still reports ceiling for caller introspection (telemetry)
  §13 Source-level pin: ceiling check fires BEFORE variance math
"""
from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.dw_heavy_probe import (
    HeavyProbeBudget, HeavyProber,
)
from backend.core.ouroboros.governance.dw_ttft_observer import (
    TtftObserver, _promotion_ceiling_ms,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_observer(tmp_path, monkeypatch) -> TtftObserver:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_TTFT_STATE_PATH",
        str(tmp_path / "ttft.json"),
    )
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED", "true")
    obs = TtftObserver()
    obs.load()
    return obs


@pytest.fixture
def isolated_budget(tmp_path, monkeypatch) -> HeavyProbeBudget:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_PATH",
        str(tmp_path / "budget.json"),
    )
    monkeypatch.setenv("JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED", "true")
    bud = HeavyProbeBudget()
    bud.load()
    return bud


# ---------------------------------------------------------------------------
# §1 — promotion_ceiling_ms env reader
# ---------------------------------------------------------------------------


def test_promotion_ceiling_default(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_TOPOLOGY_TTFT_PROMOTION_CEILING_MS", raising=False,
    )
    assert _promotion_ceiling_ms() == 5000


@pytest.mark.parametrize("val,expected", [
    ("3000", 3000), ("10000", 10000), ("100", 100),
])
def test_promotion_ceiling_env_override(monkeypatch, val, expected) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_PROMOTION_CEILING_MS", val)
    assert _promotion_ceiling_ms() == expected


@pytest.mark.parametrize("val", ["garbage", "", "  "])
def test_promotion_ceiling_invalid_falls_back_to_default(
    monkeypatch, val,
) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_PROMOTION_CEILING_MS", val)
    assert _promotion_ceiling_ms() == 5000


# ---------------------------------------------------------------------------
# §2-§4 — The bug we're fixing: uniform-timeout false positive
# ---------------------------------------------------------------------------


def test_ceiling_rejects_uniform_30s_timeouts(
    isolated_observer: TtftObserver,
) -> None:
    """THE BUG: a model returning uniform 30-second timeouts has
    CV=0, rel_SEM=0, both pass variance gates trivially. Slice G
    rejects this via the absolute ceiling. Empirical evidence from
    bt-2026-04-28-201119: DeepSeek-OCR-2 had two 30000ms samples
    after Slice D's ceiling-on-failure (now removed)."""
    for _ in range(3):
        isolated_observer.record_ttft("vendor/dead-model", 30000)
    s = isolated_observer.stats("vendor/dead-model")
    assert s is not None
    assert s.mean_ms == 30000
    assert s.cv == 0.0  # uniform → zero variance
    assert s.rel_sem == 0.0
    # Without ceiling: CV<0.15 ✓ AND rel_SEM<0.05 ✓ → would WRONGLY return True
    # With ceiling: 30000 > 5000 → returns False (correctly rejects)
    assert isolated_observer.is_promotion_ready("vendor/dead-model") is False


def test_ceiling_rejects_mean_at_ceiling(
    isolated_observer: TtftObserver,
) -> None:
    """Edge case: mean_ms == ceiling. The ``>=`` comparison rejects
    this (boundary-inclusive on the rejection side — defensive)."""
    for _ in range(3):
        isolated_observer.record_ttft("vendor/at-ceiling", 5000)
    assert isolated_observer.is_promotion_ready("vendor/at-ceiling") is False


def test_ceiling_rejects_mean_above_ceiling(
    isolated_observer: TtftObserver,
) -> None:
    """mean_ms > ceiling → rejected even with perfect consistency."""
    for ms in (5500, 5500, 5500):
        isolated_observer.record_ttft("vendor/slow", ms)
    assert isolated_observer.is_promotion_ready("vendor/slow") is False


def test_ceiling_admits_mean_below_ceiling(
    isolated_observer: TtftObserver,
) -> None:
    """A genuinely fast + consistent model passes all three gates."""
    for ms in (100, 102, 99, 101, 100):
        isolated_observer.record_ttft("vendor/fast-warm", ms)
    s = isolated_observer.stats("vendor/fast-warm")
    assert s is not None
    assert s.mean_ms < 5000
    assert s.cv < 0.15
    assert s.rel_sem < 0.05
    assert isolated_observer.is_promotion_ready("vendor/fast-warm") is True


# ---------------------------------------------------------------------------
# §5-§6 — Composition with existing gates
# ---------------------------------------------------------------------------


def test_ceiling_composes_with_cv_gate(
    isolated_observer: TtftObserver,
) -> None:
    """A model below ceiling but with high CV still rejected."""
    for ms in (400, 1200, 600, 1800, 800):  # mean ~960, high variance
        isolated_observer.record_ttft("vendor/noisy-fast", ms)
    s = isolated_observer.stats("vendor/noisy-fast")
    assert s is not None
    assert s.mean_ms < 5000  # passes ceiling
    assert s.cv > 0.15  # fails CV gate
    assert isolated_observer.is_promotion_ready("vendor/noisy-fast") is False


def test_ceiling_composes_with_rel_sem_gate(
    isolated_observer: TtftObserver,
    monkeypatch,
) -> None:
    """A model below ceiling + low CV but too few samples still rejected.
    rel_SEM = CV / sqrt(N), so small N fails the gate."""
    # 2 samples, low CV but rel_SEM = CV / sqrt(2) might exceed threshold
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_REL_SEM_THRESHOLD", "0.001")
    for ms in (100, 110):
        isolated_observer.record_ttft("vendor/tight-N2", ms)
    s = isolated_observer.stats("vendor/tight-N2")
    assert s is not None
    assert s.mean_ms < 5000  # passes ceiling
    assert s.cv < 0.15      # passes CV
    # rel_SEM with the tightened threshold must fail
    assert isolated_observer.is_promotion_ready("vendor/tight-N2") is False


# ---------------------------------------------------------------------------
# §7 — Operator override
# ---------------------------------------------------------------------------


def test_ceiling_respects_env_override_lower(
    isolated_observer: TtftObserver, monkeypatch,
) -> None:
    """Operator can lower the ceiling for stricter promotion gating
    (e.g., for latency-sensitive deployments)."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_PROMOTION_CEILING_MS", "200")
    for ms in (300, 305, 295):
        isolated_observer.record_ttft("vendor/borderline", ms)
    # Default ceiling 5000 → would promote
    # Lowered ceiling 200 → mean (≈300) > 200 → reject
    assert isolated_observer.is_promotion_ready("vendor/borderline") is False


def test_ceiling_respects_env_override_higher(
    isolated_observer: TtftObserver, monkeypatch,
) -> None:
    """Operator can raise the ceiling for endpoints that legitimately
    have high baseline latency (rare but legal)."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_PROMOTION_CEILING_MS", "10000")
    for ms in (8000, 8100, 7900, 8050):
        isolated_observer.record_ttft("vendor/slow-but-stable", ms)
    s = isolated_observer.stats("vendor/slow-but-stable")
    assert s is not None
    assert s.mean_ms < 10000  # under raised ceiling
    assert s.cv < 0.15
    assert isolated_observer.is_promotion_ready(
        "vendor/slow-but-stable",
    ) is True


# ---------------------------------------------------------------------------
# §8-§10 — Heavy probe failure ignorance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_500_does_not_feed_observer(
    isolated_budget: HeavyProbeBudget,
) -> None:
    """A 500 response is a server error, not a TTFT measurement."""
    fake_session = _SessionWith(status=500, chunks=())
    fake_obs = MagicMock()
    prober = HeavyProber(budget=isolated_budget)
    result = await prober.probe(
        session=fake_session, model_id="vendor/m-7B",
        base_url="https://test.example", api_key="key",
        observer=fake_obs,
    )
    assert result.success is False
    fake_obs.record_ttft.assert_not_called()


@pytest.mark.asyncio
async def test_failure_transport_does_not_feed_observer(
    isolated_budget: HeavyProbeBudget,
) -> None:
    """A transport-class error (connection reset, timeout) is a network
    failure, not a TTFT measurement. Observer MUST NOT receive a
    sample — would poison the rolling stats with the 30s ceiling
    pattern that triggered Slice G."""
    fake_session = _SessionRaises(_ConnectionResetError())
    fake_obs = MagicMock()
    prober = HeavyProber(budget=isolated_budget)
    result = await prober.probe(
        session=fake_session, model_id="vendor/m-7B",
        base_url="https://test.example", api_key="key",
        observer=fake_obs,
    )
    assert result.success is False
    fake_obs.record_ttft.assert_not_called()


@pytest.mark.asyncio
async def test_failure_empty_stream_does_not_feed_observer(
    isolated_budget: HeavyProbeBudget,
) -> None:
    """[DONE] arriving before any content chunk = empty stream.
    Server responded but didn't emit content. NOT a TTFT measurement."""
    fake_session = _SessionWith(
        chunks=(b"data: [DONE]\n",),
    )
    fake_obs = MagicMock()
    prober = HeavyProber(budget=isolated_budget)
    result = await prober.probe(
        session=fake_session, model_id="vendor/m-7B",
        base_url="https://test.example", api_key="key",
        observer=fake_obs,
    )
    assert result.success is False
    fake_obs.record_ttft.assert_not_called()


# ---------------------------------------------------------------------------
# §11-§12 — Success path preserved + result introspection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_still_feeds_observer(
    isolated_budget: HeavyProbeBudget,
) -> None:
    """Slice G changes failure semantics; success path still records
    the real TTFT. Pin the success path is preserved."""
    fake_session = _SessionWith(
        chunks=(b"data: {\"choices\":[{\"delta\":{\"content\":\"H\"}}]}\n",),
    )
    fake_obs = MagicMock()
    prober = HeavyProber(budget=isolated_budget)
    result = await prober.probe(
        session=fake_session, model_id="vendor/m-7B",
        base_url="https://test.example", api_key="key",
        observer=fake_obs,
    )
    assert result.success is True
    fake_obs.record_ttft.assert_called_once()


@pytest.mark.asyncio
async def test_failure_result_still_reports_ceiling_ttft(
    isolated_budget: HeavyProbeBudget, monkeypatch,
) -> None:
    """The result.ttft_ms field still reports the timeout ceiling on
    failure for caller introspection (logs, telemetry, dashboards).
    Only the OBSERVER feed is suppressed; the RESULT is for human-
    readable diagnostics."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_HEAVY_PROBE_TIMEOUT_S", "8")
    fake_session = _SessionWith(status=503, chunks=())
    prober = HeavyProber(budget=isolated_budget)
    result = await prober.probe(
        session=fake_session, model_id="vendor/m-7B",
        base_url="https://test.example", api_key="key",
    )
    assert result.success is False
    assert result.ttft_ms == 8000  # ceiling for caller introspection


# ---------------------------------------------------------------------------
# §13 — Source-level pin: ceiling check ordering
# ---------------------------------------------------------------------------


def test_ceiling_check_fires_before_variance_math() -> None:
    """Source-level pin: the ceiling check MUST fire BEFORE the CV/
    rel_SEM math. Otherwise the variance gates would short-circuit
    the path and never reach the ceiling. Pin source ordering so a
    refactor can't accidentally invert the gates."""
    src = inspect.getsource(TtftObserver.is_promotion_ready)
    ceiling_idx = src.index("_promotion_ceiling_ms()")
    cv_idx = src.index("_cv_threshold()")
    sem_idx = src.index("_rel_sem_threshold()")
    assert ceiling_idx < cv_idx, (
        "Slice G: ceiling check must fire BEFORE CV gate"
    )
    assert ceiling_idx < sem_idx, (
        "Slice G: ceiling check must fire BEFORE rel_SEM gate"
    )


def test_ceiling_check_documented_in_docstring() -> None:
    """The is_promotion_ready docstring documents the three-gate
    composition (ceiling + CV + rel_SEM) so future readers don't
    re-introduce the uniform-timeout false positive."""
    doc = TtftObserver.is_promotion_ready.__doc__
    assert doc is not None
    assert "Absolute ceiling" in doc or "ceiling" in doc.lower()
    assert "Slice G" in doc or "promotion_ceiling" in doc


# ---------------------------------------------------------------------------
# Helpers (mirror those in test_dw_heavy_probe.py)
# ---------------------------------------------------------------------------


class _SessionWith:
    def __init__(self, *, status: int = 200, chunks=()) -> None:
        self._status = status
        self._chunks = list(chunks) + [b""]

    def post(self, *a, **kw):
        return _RespCM(self._status, self._chunks)


class _SessionRaises:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def post(self, *a, **kw):
        raise self._exc


class _ConnectionResetError(OSError):
    def __init__(self) -> None:
        super().__init__(54, "Connection reset by peer")


class _RespCM:
    def __init__(self, status, chunks):
        self._status = status
        self._chunks = chunks

    async def __aenter__(self):
        return _Resp(self._status, self._chunks)

    async def __aexit__(self, *a):
        return None


class _Resp:
    def __init__(self, status, chunks):
        self.status = status
        self.content = _Content(chunks)

    async def text(self) -> str:
        return f"HTTP {self.status}"


class _Content:
    def __init__(self, chunks):
        self._chunks = chunks

    async def readline(self) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)
