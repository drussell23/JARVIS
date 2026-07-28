"""The Adaptive Payload Router — the lane is a property of the payload.

The mandated contract:

  1. a short query routes to the CLAUDE provider (low TTFT — a two-word
     question on the async tier is not a conversation);
  2. a massive query routes to the DW provider AND tells the operator why
     they are about to wait.

Plus the property that keeps a lane choice from becoming an outage: the
preference is a BIAS, not a pin — the other tier is still the fallback, so
a heavy query still gets answered when DW is down.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import adaptive_lane_router as alr


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (alr.MASTER_FLAG_ENV_VAR, alr.HEAVY_TOKENS_ENV_VAR,
                alr.TTFT_HINT_ENV_VAR):
        monkeypatch.delenv(var, raising=False)
    yield


_SHORT = "can you tell me what is O+V?"
#: ~12.5k characters ≈ 3.1k tokens — comfortably past the 2k boundary.
_HUGE = "x" * 12_500


# --------------------------------------------------------------------------
# 1. the estimate
# --------------------------------------------------------------------------

def test_estimate_uses_the_repo_heuristic() -> None:
    assert alr.estimate_payload_tokens("a" * 4000) == 1000


def test_estimate_walks_every_part_and_sequence() -> None:
    """Attachments arrive as a list; each one is payload."""
    both = alr.estimate_payload_tokens("a" * 400, ["b" * 400, "c" * 400])
    assert both == 300


def test_estimate_survives_junk() -> None:
    assert alr.estimate_payload_tokens(None, 42, object()) >= 0


# --------------------------------------------------------------------------
# 2. the rule
# --------------------------------------------------------------------------

def test_short_payload_takes_the_fast_lane() -> None:
    assert alr.choose_lane(alr.estimate_payload_tokens(_SHORT)) is alr.Lane.FAST


def test_massive_payload_takes_the_heavy_lane() -> None:
    assert alr.choose_lane(alr.estimate_payload_tokens(_HUGE)) is alr.Lane.HEAVY


def test_boundary_is_inclusive_and_live(monkeypatch) -> None:
    monkeypatch.setenv(alr.HEAVY_TOKENS_ENV_VAR, "100")
    assert alr.choose_lane(99) is alr.Lane.FAST
    assert alr.choose_lane(100) is alr.Lane.HEAVY
    monkeypatch.setenv(alr.HEAVY_TOKENS_ENV_VAR, "1000")
    assert alr.choose_lane(100) is alr.Lane.FAST      # no restart needed


def test_misconfiguration_degrades_toward_responsiveness(
    monkeypatch,
) -> None:
    """A bad threshold must never mean 'wait 60s on every keystroke'."""
    monkeypatch.setenv(alr.HEAVY_TOKENS_ENV_VAR, "not-a-number")
    assert alr.choose_lane(10_000) is alr.Lane.HEAVY   # default still applies
    monkeypatch.setenv(alr.HEAVY_TOKENS_ENV_VAR, "0")
    assert alr.choose_lane(10_000) is alr.Lane.FAST    # 0 = never heavy
    monkeypatch.setenv(alr.HEAVY_TOKENS_ENV_VAR, "-5")
    assert alr.choose_lane(10_000) is alr.Lane.FAST


def test_master_flag_off_defers_to_global_policy(monkeypatch) -> None:
    monkeypatch.setenv(alr.MASTER_FLAG_ENV_VAR, "false")
    assert alr.choose_lane(999_999) is alr.Lane.FAST
    assert alr.prefer_for(alr.Lane.FAST) is None       # gate decides


def test_prefer_tokens_match_the_gate_vocabulary() -> None:
    assert alr.prefer_for(alr.Lane.FAST) == "claude"
    assert alr.prefer_for(alr.Lane.HEAVY) == "dw"


# --------------------------------------------------------------------------
# 3. THE MANDATED CONTRACT — routing + the UX notice, async
# --------------------------------------------------------------------------

def test_short_routes_to_claude_and_huge_routes_to_dw_with_notice() -> None:
    """Drives the real gate with fake providers, asserting WHICH one was
    consulted first — and that the loop never blocks."""
    from backend.core.ouroboros.governance import rt_gate

    async def scenario() -> None:
        ticks = {"n": 0}

        async def _heartbeat() -> None:
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.005)

        beat = asyncio.ensure_future(_heartbeat())
        try:
            for payload, expect_first, expect_heavy in (
                (_SHORT, "claude", False),
                (_HUGE, "dw", True),
            ):
                order: list = []
                notices: list = []

                async def _fake_claude(*_a, **_k):
                    order.append("claude")
                    return "claude-answer"

                async def _fake_dw(*_a, **_k):
                    order.append("dw")
                    return "dw-answer"

                decision = alr.route(payload, notify=notices.append)
                assert decision.is_heavy is expect_heavy

                # Patch the tier attempts, not the transport: the gate's
                # own ordering logic is what we are asserting on.
                import unittest.mock as mock
                with mock.patch.object(rt_gate, "_try_claude", _fake_claude), \
                        mock.patch.object(rt_gate, "_try_dw_rt", _fake_dw):
                    answer = await rt_gate.gate_completion(
                        payload, caller_id="test",
                        prefer=decision.prefer,
                    )
                assert order[0] == expect_first, (payload[:20], order)
                assert answer == f"{expect_first}-answer"

                if expect_heavy:
                    assert notices and "🐢" in notices[0]
                    assert "async DW lane" in notices[0]
                    assert alr.ttft_hint() in notices[0]
                else:
                    assert notices == []      # silence when nothing is odd

            await asyncio.sleep(0.02)
            assert ticks["n"] > 1             # the loop kept turning
        finally:
            beat.cancel()

    asyncio.run(scenario())


def test_preference_is_a_bias_not_a_pin() -> None:
    """A heavy query when DW is DOWN must still be answered by Claude —
    a lane choice can never become a single point of failure."""
    from backend.core.ouroboros.governance import rt_gate
    import unittest.mock as mock

    async def scenario() -> str:
        async def _dead_dw(*_a, **_k):
            return None                      # tier unavailable

        async def _live_claude(*_a, **_k):
            return "claude-rescued"

        with mock.patch.object(rt_gate, "_try_dw_rt", _dead_dw), \
                mock.patch.object(rt_gate, "_try_claude", _live_claude):
            return await rt_gate.gate_completion(
                _HUGE, caller_id="test", prefer="dw",
            )

    assert asyncio.run(scenario()) == "claude-rescued"


def test_unset_prefer_keeps_the_global_policy() -> None:
    from backend.core.ouroboros.governance import rt_gate
    import unittest.mock as mock

    async def scenario() -> list:
        order: list = []

        async def _c(*_a, **_k):
            order.append("claude")
            return "a"

        async def _d(*_a, **_k):
            order.append("dw")
            return "a"

        with mock.patch.object(rt_gate, "_try_claude", _c), \
                mock.patch.object(rt_gate, "_try_dw_rt", _d):
            await rt_gate.gate_completion("hi", caller_id="t", prefer=None)
        return order

    # Default global policy is claude-first; the point is that None does
    # not silently become a preference.
    assert asyncio.run(scenario())[0] == "claude"


# --------------------------------------------------------------------------
# 4. the notice + the decision record
# --------------------------------------------------------------------------

def test_notice_explains_why_where_and_the_cost(monkeypatch) -> None:
    monkeypatch.setenv(alr.TTFT_HINT_ENV_VAR, "~90s")
    text = alr.heavy_notice(3100)
    assert "3.1k tokens" in text          # why
    assert "DW" in text                   # where
    assert "~90s" in text                 # what it costs them


def test_route_returns_an_inspectable_decision() -> None:
    d = alr.route(_HUGE)
    assert d.to_dict()["lane"] == "heavy"
    assert d.to_dict()["prefer"] == "dw"
    assert d.to_dict()["estimated_tokens"] > 2000
    assert d.to_dict()["schema_version"] == alr.ADAPTIVE_LANE_SCHEMA_VERSION


def test_a_failing_notify_never_breaks_routing() -> None:
    def _boom(_m):
        raise RuntimeError("hud gone")

    assert alr.route(_HUGE, notify=_boom).is_heavy is True


# --------------------------------------------------------------------------
# 5. wiring
# --------------------------------------------------------------------------

def test_karen_measures_the_grounded_prompt_and_passes_prefer() -> None:
    import inspect
    from backend.core.ouroboros.governance import karen_answer_engine
    src = inspect.getsource(karen_answer_engine.KarenQueryProvider.query)
    assert "_route_lane(" in src
    assert "grounded" in src.split("_route_lane(")[1][:80]   # the real wire
    assert "prefer=_decision.prefer" in src


def test_gate_exposes_a_per_call_preference() -> None:
    import inspect
    from backend.core.ouroboros.governance import rt_gate
    sig = inspect.signature(rt_gate.gate_completion)
    assert "prefer" in sig.parameters
    assert sig.parameters["prefer"].default is None
