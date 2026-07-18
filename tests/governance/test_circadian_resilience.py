"""Circadian Resilience — telemetry-driven quota shaping + provider-aware
hibernation.

The mandate's core assertions:
  * a 200 OK whose headers declare only 500 tokens remain makes the
    SensorGovernor PREEMPTIVELY pause a BULK (BACKGROUND) operation — before
    any 429 is ever triggered — while CRITICAL/IMMEDIATE lanes keep their
    runway;
  * hibernation entry checkpoints in-flight state (capture_inflight — FSM +
    atomic stash) without crashing the event loop, and the prober wakes the
    organism when the provider returns healthy, resuming operations;
  * clock skew cannot corrupt the horizon: durations come from RELATIVE
    deltas (retry-after; reset-vs-server-Date), never local-vs-server
    absolute comparison.
"""
from __future__ import annotations

import asyncio
import time
from email.utils import formatdate

import pytest

from backend.core.ouroboros.governance import provider_liquidity_ledger as pll
from backend.core.ouroboros.governance.sensor_governor import (
    SensorBudgetSpec,
    SensorGovernor,
    Urgency,
)


@pytest.fixture(autouse=True)
def _ledger_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROVIDER_LIQUIDITY_PATH", str(tmp_path / "liquidity.json"),
    )
    for v in ("JARVIS_LIQUIDITY_MIN_TOKENS_FLOOR",
              "JARVIS_LIQUIDITY_BACKPRESSURE_ENABLED",
              "JARVIS_PROVIDER_LIQUIDITY_ENABLED"):
        monkeypatch.delenv(v, raising=False)
    pll._reset_for_tests()
    yield
    pll._reset_for_tests()


def _headers_200_low_tokens(remaining=500, reset_in_s=120.0, server_offset_s=0.0):
    """A 200-OK header set: server declares `remaining` tokens, resetting
    `reset_in_s` from the SERVER's own clock (which may be skewed vs local)."""
    server_now = time.time() + server_offset_s
    from datetime import datetime, timezone
    reset_iso = datetime.fromtimestamp(
        server_now + reset_in_s, tz=timezone.utc,
    ).isoformat()
    return {
        "anthropic-ratelimit-tokens-remaining": str(remaining),
        "anthropic-ratelimit-tokens-reset": reset_iso,
        "Date": formatdate(server_now, usegmt=True),
        "Content-Type": "application/json",
    }


# ===========================================================================
# A. Ledger — skew-safe parsing
# ===========================================================================


def test_record_and_read_declared_liquidity():
    assert pll.record_headers("anthropic", _headers_200_low_tokens()) is True
    tokens, secs = pll.liquidity("anthropic")
    assert tokens == 500
    assert secs is not None and 110.0 < secs <= 121.5


def test_clock_skew_is_neutralized_by_server_date():
    """Server clock 10 MINUTES ahead of local: the reset delta must still be
    ~120s (server-vs-server), not ~720s (absolute-vs-local)."""
    pll.record_headers(
        "anthropic",
        _headers_200_low_tokens(reset_in_s=120.0, server_offset_s=600.0),
    )
    _t, secs = pll.liquidity("anthropic")
    assert secs is not None and 110.0 < secs <= 121.0, (
        f"skew leaked into the horizon: {secs}"
    )


def test_retry_after_takes_precedence():
    h = _headers_200_low_tokens(reset_in_s=300.0)
    h["Retry-After"] = "45"
    pll.record_headers("anthropic", h, status=429)
    _t, secs = pll.liquidity("anthropic")
    assert secs is not None and 40.0 < secs <= 45.5


def test_runway_exhausted_truth_table():
    pll.record_headers("anthropic", _headers_200_low_tokens(remaining=500))
    assert pll.runway_exhausted("anthropic") is True          # 500 < 2000 floor
    pll.record_headers("anthropic", _headers_200_low_tokens(remaining=50_000))
    assert pll.runway_exhausted("anthropic") is False         # ample
    assert pll.runway_exhausted("anthropic", forecast_tokens=60_000) is True
    assert pll.runway_exhausted("unknown-provider") is False  # no data → open


def test_exhaustion_expires_at_declared_reset():
    # Retry-After (pure relative delta) so the tiny horizon isn't quantized by
    # the Date header's whole-second resolution.
    pll.record_headers(
        "anthropic",
        {"anthropic-ratelimit-tokens-remaining": "500", "Retry-After": "0.05"},
    )
    assert pll.runway_exhausted("anthropic") is True
    time.sleep(0.08)
    pll._reset_for_tests()                                     # drop read cache
    assert pll.runway_exhausted("anthropic") is False          # self-releasing


def test_no_headers_records_nothing():
    assert pll.record_headers("anthropic", {"Content-Type": "text/html"}) is False


# ===========================================================================
# B. THE mandate test — 200 OK / 500 tokens left → governor pauses BULK
#    before any 429 exists anywhere.
# ===========================================================================


def test_governor_preemptively_pauses_bulk_on_declared_low_tokens():
    # ONE 200-OK response is recorded. No 429 has ever occurred.
    pll.record_headers(
        "anthropic", _headers_200_low_tokens(remaining=500), status=200,
    )
    gov = SensorGovernor()   # default liquidity fn reads the REAL ledger file
    gov.register(SensorBudgetSpec(
        sensor_name="bulk_test_sensor", base_cap_per_hour=100,
    ))
    gov.register(SensorBudgetSpec(
        sensor_name="critical_test_sensor", base_cap_per_hour=100,
    ))

    bulk = gov.request_budget("bulk_test_sensor", Urgency.BACKGROUND)
    assert bulk.allowed is False, "BULK must be PAUSED preemptively"
    assert bulk.reason_code == "governor.liquidity_backpressure"

    spec_lane = gov.request_budget("bulk_test_sensor", Urgency.SPECULATIVE)
    assert spec_lane.allowed is False

    critical = gov.request_budget("critical_test_sensor", Urgency.IMMEDIATE)
    assert critical.allowed is True, "CRITICAL keeps its runway — that's the point"


def test_governor_releases_when_liquidity_recovers():
    pll.record_headers("anthropic", _headers_200_low_tokens(remaining=500))
    gov = SensorGovernor()
    gov.register(SensorBudgetSpec(sensor_name="s", base_cap_per_hour=100))
    assert (gov.request_budget("s", Urgency.BACKGROUND)).allowed is False
    # Provider declares ample tokens on the next 200 → shaping self-releases.
    pll.record_headers("anthropic", _headers_200_low_tokens(remaining=80_000))
    pll._reset_for_tests()
    assert (gov.request_budget("s", Urgency.BACKGROUND)).allowed is True


def test_kill_switch_restores_legacy(monkeypatch):
    monkeypatch.setenv("JARVIS_LIQUIDITY_BACKPRESSURE_ENABLED", "false")
    pll.record_headers("anthropic", _headers_200_low_tokens(remaining=1))
    gov = SensorGovernor()
    gov.register(SensorBudgetSpec(sensor_name="s", base_cap_per_hour=100))
    assert (gov.request_budget("s", Urgency.BACKGROUND)).allowed is True


# ===========================================================================
# C. Hibernation loop — checkpoint on entry, wake on recovery, loop alive
# ===========================================================================


@pytest.mark.asyncio
async def test_hibernate_checkpoints_and_wakes_on_recovery(monkeypatch, tmp_path):
    """Global exhaustion → controller hibernates; the entry hook runs
    capture_inflight (FSM + stash primitive) without crashing the loop; the
    prober polls the mock provider (unhealthy → healthy) and wakes; the wake
    hook re-hydrates checkpoints; mode is restored."""
    from backend.core.ouroboros.governance.supervisor_controller import (
        SupervisorOuroborosController,
    )
    from backend.core.ouroboros.governance.provider_exhaustion_watcher import (
        ProviderExhaustionWatcher,
    )
    from backend.core.ouroboros.governance.hibernation_prober import (
        HibernationProber,
    )

    captured = {"n": 0}
    hydrated = {"n": 0}

    def _fake_capture(**kw):
        captured["n"] += 1
        return 1

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.fsm_checkpoint.capture_inflight",
        _fake_capture,
    )

    ctrl = SupervisorOuroborosController()
    from backend.core.ouroboros.governance.supervisor_controller import (
        AutonomyMode as _AM,
    )
    ctrl._mode = _AM.GOVERNED   # unit test: bypass activation gates
    # Register hooks with the SAME shape the GLS bridges use (entry →
    # capture_inflight; wake → checkpoint hydration).
    def _on_hibernate(*, reason: str) -> None:
        from backend.core.ouroboros.governance.fsm_checkpoint import (
            capture_inflight,
        )
        capture_inflight(reason=f"provider_hibernation:{reason}")

    async def _on_wake(*, reason: str) -> None:
        hydrated["n"] += 1

    ctrl.register_hibernation_hooks(
        on_hibernate=_on_hibernate, on_wake=_on_wake, name="test",
    )

    class _Provider:
        name = "mock"
        calls = 0
        async def health_probe(self):
            _Provider.calls += 1
            return _Provider.calls >= 2          # 1st probe: 429-dark; 2nd: 200 OK

    monkeypatch.setenv("JARVIS_HIBERNATION_PROBE_INITIAL_S", "0.05")
    prober = HibernationProber(controller=ctrl, providers=[_Provider()])
    watcher = ProviderExhaustionWatcher(controller=ctrl, threshold=1)
    watcher.attach_prober(prober)

    # Global mesh exhaustion (the mock 429/exhaustion event).
    await watcher.record_exhaustion(reason="all_providers_exhausted", op_id="op-1")

    from backend.core.ouroboros.governance.supervisor_controller import AutonomyMode
    assert ctrl.mode is AutonomyMode.HIBERNATION
    assert captured["n"] == 1, "entry must checkpoint via capture_inflight"

    # The loop is NOT crashed — we're still executing on it; wait for wake.
    for _ in range(100):
        await asyncio.sleep(0.05)
        if ctrl.mode is not AutonomyMode.HIBERNATION:
            break
    assert ctrl.mode is not AutonomyMode.HIBERNATION, "prober must wake on 200 OK"
    # The wake hook is ASYNC and fires concurrently with the mode flip —
    # grace-poll its completion instead of racing it.
    for _ in range(40):
        if hydrated["n"]:
            break
        await asyncio.sleep(0.05)
    assert hydrated["n"] == 1, "wake must trigger checkpoint re-hydration"


def test_prober_seeds_first_delay_from_declared_reset(monkeypatch):
    pll.record_headers(
        "anthropic", _headers_200_low_tokens(remaining=100, reset_in_s=90.0),
    )
    from backend.core.ouroboros.governance.hibernation_prober import (
        HibernationProber,
    )
    monkeypatch.setenv("JARVIS_HIBERNATION_PROBE_INITIAL_S", "5")
    monkeypatch.setenv("JARVIS_HIBERNATION_PROBE_MAX_S", "300")
    prober = HibernationProber(controller=None, providers=[])
    d = prober._first_probe_delay()
    assert 80.0 < d <= 91.0, f"first delay must honor the declared horizon: {d}"


# ===========================================================================
# D. Aegis classification helper
# ===========================================================================


def test_provider_classification():
    assert pll.provider_for_upstream("/v1/messages") == "anthropic"
    assert pll.provider_for_upstream("api.anthropic.com/v1/messages") == "anthropic"
    assert pll.provider_for_upstream("/chat/completions") == "doubleword"
    assert pll.provider_for_upstream("") == "unknown"


# ===========================================================================
# E. Liquidity Ping guard — wake yields until tokens replenish
# ===========================================================================


@pytest.mark.asyncio
async def test_wake_yields_until_liquidity_replenishes(monkeypatch):
    """Double-fault-429 neutralization: the provider answers the carry-proof
    (stable) but the ledger still declares an exhausted runway → the prober
    STAYS DARK. When the ledger shows replenished tokens, the next probe
    wakes."""
    from backend.core.ouroboros.governance.supervisor_controller import (
        AutonomyMode as _AM,
        SupervisorOuroborosController,
    )
    from backend.core.ouroboros.governance.hibernation_prober import (
        HibernationProber,
    )

    ctrl = SupervisorOuroborosController()
    ctrl._mode = _AM.GOVERNED
    await ctrl.enter_hibernation("test: quota dark window")

    class _Provider:
        name = "mock"
        async def health_probe(self):
            return True                              # always answers (stable sliver)

    monkeypatch.setenv("JARVIS_HIBERNATION_PROBE_INITIAL_S", "0.05")
    prober = HibernationProber(controller=ctrl, providers=[_Provider()])
    # Carry-proof always passes; the LEDGER is the gate under test.
    async def _stable(*a, **k):
        return True
    monkeypatch.setattr(prober, "_verify_grid_stability", _stable)

    exhausted = {"v": True}
    monkeypatch.setattr(prober, "_liquidity_still_exhausted", lambda: exhausted["v"])

    await prober.start()
    # Phase 1: runway still exhausted → several probes, NO wake.
    await asyncio.sleep(0.4)
    assert ctrl.mode is _AM.HIBERNATION, "must stay dark on unreplenished runway"
    assert prober._last_result == "liquidity_not_replenished"

    # Phase 2: tokens replenish → next probe wakes.
    exhausted["v"] = False
    for _ in range(60):
        await asyncio.sleep(0.05)
        if ctrl.mode is not _AM.HIBERNATION:
            break
    assert ctrl.mode is not _AM.HIBERNATION, "must wake once runway replenishes"


def test_liquidity_guard_fails_open_without_ledger(monkeypatch, tmp_path):
    """No telemetry (ledger absent) → the guard must NOT strand the organism."""
    from backend.core.ouroboros.governance.hibernation_prober import (
        HibernationProber,
    )
    monkeypatch.setenv(
        "JARVIS_PROVIDER_LIQUIDITY_PATH", str(tmp_path / "absent.json"),
    )
    pll._reset_for_tests()
    prober = HibernationProber(controller=None, providers=[])
    assert prober._liquidity_still_exhausted() is False
