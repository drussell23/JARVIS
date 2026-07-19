"""Dynamic QoS Priority Escalation (Aging) — starvation-math spine.

Pins the four mandates: (1) no timers — aging is a pure function of
lazily-evaluated age; (2) throttled envelopes shunt + age + escalate
past the weighted cap under a one-shot grant; (3) middleware over the
existing router/ledger seams (integration tests run the REAL ingest
path); (4) anti-inversion — pledges cap at the 30% ratio of the
replenished window's declared tokens, and exhausted liquidity blocks
escalation entirely.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from backend.core.ouroboros.governance import qos_starvation as qs


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("JARVIS_QOS_ESCALATION_ENABLED", "true")
    for k in (
        "JARVIS_QOS_AGING_MODE", "JARVIS_QOS_AGING_TICK_S",
        "JARVIS_QOS_AGING_SLOPE", "JARVIS_QOS_AGING_GROWTH",
        "JARVIS_QOS_ESCALATION_THRESHOLD", "JARVIS_QOS_STARVATION_MAX",
        "JARVIS_QOS_STARVATION_MAX_RATIO", "JARVIS_QOS_PLEDGE_TOKENS_EST",
    ):
        monkeypatch.delenv(k, raising=False)
    qs.reset_default_ledger()
    yield
    qs.reset_default_ledger()


@pytest.fixture()
def liquidity(tmp_path, monkeypatch):
    """Writable liquidity ledger seam (the REAL provider_liquidity_ledger)."""
    from backend.core.ouroboros.governance import provider_liquidity_ledger as pl
    path = tmp_path / "liq.json"
    monkeypatch.setenv("JARVIS_PROVIDER_LIQUIDITY_PATH", str(path))
    pl._reset_for_tests()

    def write(tokens: int, reset_s: float = 300.0):
        path.write_text(json.dumps({
            "schema_version": pl.PROVIDER_LIQUIDITY_SCHEMA_VERSION,
            "providers": {"anthropic": {
                "tokens_remaining": tokens, "reset_delta_s": reset_s,
                "recorded_unix": time.time(), "last_status": 200,
            }},
        }))
        pl._reset_for_tests()

    yield write
    pl._reset_for_tests()


class _Env:
    def __init__(self, cid: str, source: str = "exploration") -> None:
        self.causal_id = cid
        self.source = source


# ---------------------------------------------------------------------------
# (1) Aging math — pure, no timers
# ---------------------------------------------------------------------------


def test_linear_aging_defaults():
    # weight = 1 + 0.5 * (age/60): fresh=1.0, 2min=2.0, 4min=3.0
    assert qs.aged_weight(0) == pytest.approx(1.0)
    assert qs.aged_weight(120) == pytest.approx(2.0)
    assert qs.aged_weight(240) == pytest.approx(3.0)


def test_exponential_aging(monkeypatch):
    monkeypatch.setenv("JARVIS_QOS_AGING_MODE", "exp")
    monkeypatch.setenv("JARVIS_QOS_AGING_GROWTH", "2.0")
    assert qs.aged_weight(0) == pytest.approx(1.0)
    assert qs.aged_weight(60) == pytest.approx(2.0)
    assert qs.aged_weight(180) == pytest.approx(8.0)


def test_aging_never_raises_and_clamps():
    assert qs.aged_weight(-100) == pytest.approx(1.0)


def test_escalation_at_threshold_lazily(liquidity):
    liquidity(tokens=1_000_000)
    led = qs.StarvationLedger()
    t0 = 1000.0
    assert led.shunt(_Env("e1"), reason="cap", now=t0)
    # 2 minutes old: weight 2.0 < 3.0 → not escalatable.
    assert led.escalatable(now=t0 + 120) == []
    # 4 minutes old: weight 3.0 → escalatable. NO ticks ever ran.
    rows = led.escalatable(now=t0 + 240)
    assert [e.causal_id for e in rows] == ["e1"]


# ---------------------------------------------------------------------------
# (2) Shunt + grant lifecycle
# ---------------------------------------------------------------------------


def test_shunt_master_off_is_noop(monkeypatch):
    monkeypatch.setenv("JARVIS_QOS_ESCALATION_ENABLED", "false")
    led = qs.StarvationLedger()
    assert led.shunt(_Env("x"), reason="cap") is False
    assert led.depth == 0


def test_shunt_bounded_refuses_youngest(monkeypatch):
    monkeypatch.setenv("JARVIS_QOS_STARVATION_MAX", "4")
    led = qs.StarvationLedger()
    for i in range(4):
        assert led.shunt(_Env(f"e{i}"), reason="cap", now=100.0 + i)
    assert led.shunt(_Env("late"), reason="cap", now=200.0) is False
    assert led.depth == 4                      # oldest starvers survive
    assert led.stats["refused_full"] == 1


def test_escalate_mints_one_shot_grant(liquidity):
    liquidity(tokens=1_000_000)
    led = qs.StarvationLedger()
    t0 = 1000.0
    led.shunt(_Env("e1"), reason="cap", now=t0)
    entry = led.try_escalate(now=t0 + 300)
    assert entry is not None and entry.causal_id == "e1"
    assert led.depth == 0                      # left the starvation queue
    assert led.consume_grant("e1", now=t0 + 301) is True
    assert led.consume_grant("e1", now=t0 + 302) is False   # one-shot


def test_grant_expires(liquidity, monkeypatch):
    liquidity(tokens=1_000_000)
    monkeypatch.setenv("JARVIS_QOS_GRANT_TTL_S", "10")
    led = qs.StarvationLedger()
    led.shunt(_Env("e1"), reason="cap", now=0.0)
    assert led.try_escalate(now=300.0) is not None
    assert led.consume_grant("e1", now=400.0) is False      # TTL passed


def test_heaviest_starver_escalates_first(liquidity):
    liquidity(tokens=1_000_000)
    led = qs.StarvationLedger()
    led.shunt(_Env("young"), reason="cap", now=1000.0)
    led.shunt(_Env("old"), reason="cap", now=0.0)
    entry = led.try_escalate(now=1300.0)
    assert entry is not None and entry.causal_id == "old"


# ---------------------------------------------------------------------------
# (3) Anti-inversion — the 30% pledge ratio + exhausted-liquidity block
# ---------------------------------------------------------------------------


def test_exhausted_liquidity_blocks_escalation(liquidity):
    liquidity(tokens=100)                       # below floor → runway DRY
    led = qs.StarvationLedger()
    led.shunt(_Env("e1"), reason="liquidity", now=0.0)
    assert led.try_escalate(now=600.0) is None  # starved but liquidity dry


def test_pledge_ratio_caps_background_claim(liquidity, monkeypatch):
    # Window: 100k declared tokens · ratio 0.30 · pledge est 20k
    # → escalation 1 (20k) OK ok, 2 (40k) > 30k ratio → DENIED.
    liquidity(tokens=100_000)
    monkeypatch.setenv("JARVIS_QOS_PLEDGE_TOKENS_EST", "20000")
    led = qs.StarvationLedger()
    led.shunt(_Env("e1"), reason="cap", now=0.0)
    led.shunt(_Env("e2"), reason="cap", now=0.0)
    assert led.try_escalate(now=600.0) is not None          # 20k pledged
    assert led.try_escalate(now=600.0) is None              # 40k > 30k cap
    assert led.stats["pledge_denied"] >= 1
    assert led.depth == 1                       # e2 keeps starving, not lost


def test_pledge_window_resets_on_replenishment_edge(liquidity, monkeypatch):
    liquidity(tokens=100_000)
    monkeypatch.setenv("JARVIS_QOS_PLEDGE_TOKENS_EST", "20000")
    led = qs.StarvationLedger()
    led.shunt(_Env("e1"), reason="cap", now=0.0)
    led.shunt(_Env("e2"), reason="cap", now=0.0)
    assert led.try_escalate(now=600.0) is not None
    assert led.try_escalate(now=600.0) is None              # ratio spent
    # Window drains dry, then a FRESH window replenishes → pledges reset.
    liquidity(tokens=100)
    assert led.try_escalate(now=700.0) is None              # dry blocks
    liquidity(tokens=200_000)
    assert led.try_escalate(now=800.0) is not None          # new window


# ---------------------------------------------------------------------------
# (4) Router integration — the REAL ingest seam
# ---------------------------------------------------------------------------


def _grep_router_src() -> str:
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    return (
        root / "backend/core/ouroboros/governance/intake/"
        "unified_intake_router.py"
    ).read_text()


def test_router_shunts_on_enforce_deny_pin():
    src = _grep_router_src()
    deny = src.index('return "governor_throttled"')
    region = src[deny - 1200:deny]
    assert "shunt(" in region                   # parked BEFORE the deny return


def test_router_consumes_grant_before_consult_pin():
    src = _grep_router_src()
    assert "consume_grant" in src
    grant = src.index("consume_grant")
    consult = src.index("self._consult_governor(envelope)", grant)
    assert consult > grant                      # grant checked first


def test_router_pump_is_reentrancy_guarded_pin():
    src = _grep_router_src()
    body = src[src.index("def _maybe_pump_qos_starvation"):][:2500]
    assert "_qos_pump_inflight" in body
    assert "try_escalate" in body


async def test_end_to_end_starve_age_escalate_execute(liquidity, monkeypatch):
    """THE telemetry loop in miniature: deny → shunt → age → healthy
    liquidity → grant → re-ingest pre-empts the weighted cap."""
    liquidity(tokens=1_000_000)
    led = qs.get_default_ledger()
    env = _Env("op-starved-1")

    # Governor denies (simulated enforce seam) → shunt instead of drop.
    assert led.shunt(env, reason="governor.liquidity_backpressure",
                     now=0.0) is True
    # Age past threshold, liquidity healthy → escalate + grant.
    entry = led.try_escalate(now=400.0)
    assert entry is not None
    # The re-ingest consult path: grant pre-empts exactly once.
    assert led.consume_grant(env.causal_id, now=401.0) is True
    # A second envelope with no grant still faces the governor.
    assert led.consume_grant("op-unstarved", now=401.0) is False
    snap = led.snapshot()
    assert snap["stats"]["shunted"] == 1
    assert snap["stats"]["escalated"] == 1
    assert snap["stats"]["grants_consumed"] == 1
