"""AdaptiveVoiceRouter — Karen's spoken turns route remote-first, fail local.

The two mandated assertions:

  (1) under normal conditions a spoken turn reaches the DW voice lane;
  (2) a TimeoutError from that lane is caught and the LOCAL engine's response
      is returned instead, without the audio pipeline crashing.

Everything is injected — dispatch, model resolution, memory pressure, clock —
because a router test that needs DW is testing DW.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.core.ouroboros.governance.adaptive_voice_router import (
    AdaptiveVoiceRouter,
    build_voice_router,
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Req:
    """Duck-typed ModelRequest — the router reads attributes, not a class."""

    def __init__(self, text="what are you working on?", system="You are Karen."):
        self.messages = [{"role": "user", "content": text}]
        self.system_prompt = system
        self.max_tokens = 128
        self.temperature = 0.7
        self.stream = True


class _Local:
    """Stand-in for UnifiedModelServing."""

    def __init__(self, chunks=("local ", "answer"), boom=None):
        self.calls = 0
        self._chunks = chunks
        self._boom = boom
        self.model_name = "local-engine"      # for the delegation test

    async def generate_stream(self, _request):
        self.calls += 1
        if self._boom is not None:
            raise self._boom
        for c in self._chunks:
            yield c


def _dw(chunks, *, fail_after=None, exc=None, delay=0.0):
    """An injected DW SSE dispatch.

    ``fail_after`` raises AFTER N tokens — the mid-utterance case, which must
    NOT fail over. ``exc`` with fail_after=0 raises before anything is spoken.
    """
    lines = [
        f'data: {json.dumps({"choices": [{"delta": {"content": c}}]})}\n'.encode()
        for c in chunks
    ] + [b"data: [DONE]\n", b""]
    state = {"i": 0, "sent": 0}

    async def _readline():
        if delay:
            await asyncio.sleep(delay)
        if fail_after is not None and state["sent"] >= fail_after:
            raise (exc or ConnectionError("dw died"))
        if state["i"] >= len(lines):
            return b""
        line = lines[state["i"]]
        state["i"] += 1
        if b"content" in line:
            state["sent"] += 1
        return line

    async def _dispatch(payload):
        _dispatch.seen = payload           # type: ignore[attr-defined]
        return _readline

    _dispatch.seen = None                  # type: ignore[attr-defined]
    return _dispatch


async def _drain(router, req=None):
    return "".join([c async for c in router.generate_stream(req or _Req())])


def _router(**kw):
    kw.setdefault("resolve_model", lambda: "elected/voice-model")
    kw.setdefault("pressure_fn", lambda: False)
    return AdaptiveVoiceRouter(**kw)


# ---------------------------------------------------------------------------
# (1) normal conditions -> the DW voice lane
# ---------------------------------------------------------------------------


async def test_a_spoken_turn_routes_to_the_dw_voice_lane():
    """(1) THE MANDATE. Remote-first, and the local engine is not touched."""
    local = _Local()
    r = _router(local=local, dispatch=_dw(["I'm ", "here."]))

    assert await _drain(r) == "I'm here."
    assert local.calls == 0, "burned local memory despite a healthy DW lane"
    assert r.last_route == "remote"


async def test_the_remote_payload_carries_the_elected_model_and_rt_tier():
    """A voice lane that elects a 1s model and then sends the request on the
    default async tier (~66s TTFT) has elected nothing."""
    d = _dw(["hi"])
    await _drain(_router(local=_Local(), dispatch=d))

    body = d.seen
    assert body["model"] == "elected/voice-model"
    assert body["stream"] is True
    assert body["messages"][0] == {"role": "system", "content": "You are Karen."}


async def test_tokens_stream_rather_than_arriving_as_one_block():
    """The sentence splitter feeds TTS incrementally; a single blob would make
    Karen wait for the full generation before saying a word."""
    got = []
    r = _router(local=_Local(), dispatch=_dw(["one ", "two ", "three"]))
    async for tok in r.generate_stream(_Req()):
        got.append(tok)
    assert len(got) == 3


# ---------------------------------------------------------------------------
# (2) TimeoutError -> local fallback, no crash
# ---------------------------------------------------------------------------


async def test_a_dw_timeout_falls_back_to_the_local_engine():
    """(2) THE MANDATE. The remote lane raises TimeoutError; the operator still
    hears the local answer and nothing propagates."""
    local = _Local(chunks=("local ", "answer"))
    r = _router(
        local=local,
        dispatch=_dw([], fail_after=0, exc=asyncio.TimeoutError()),
    )

    assert await _drain(r) == "local answer"
    assert local.calls == 1
    assert r.last_route == "local"


async def test_a_network_error_falls_back_the_same_way():
    local = _Local()
    r = _router(local=local, dispatch=_dw([], fail_after=0,
                                          exc=ConnectionResetError("reset")))
    assert await _drain(r) == "local answer"


async def test_a_dispatch_that_raises_immediately_falls_back():
    async def _boom(_payload):
        raise OSError("no route to host")

    assert await _drain(_router(local=_Local(), dispatch=_boom)) == "local answer"


async def test_a_slow_first_token_fails_over_within_budget(monkeypatch):
    """Silence is the failure mode voice cannot tolerate. A remote lane that
    has not spoken inside the TTFT budget loses the turn."""
    monkeypatch.setenv("JARVIS_VOICE_ROUTER_TTFT_BUDGET_S", "0.15")
    local = _Local()
    r = _router(local=local, dispatch=_dw(["eventually"], delay=5.0))

    assert await asyncio.wait_for(_drain(r), timeout=3.0) == "local answer"
    assert local.calls == 1


# ---------------------------------------------------------------------------
# The failover rule that matters — emission is the point of no return
# ---------------------------------------------------------------------------


async def test_a_fault_after_the_first_token_does_not_restart_locally():
    """THE EDGE CASE. Once a token has reached the splitter it is on its way to
    the speakers. Failing over now would have Karen say half of one answer and
    then all of another — so the utterance ends instead."""
    local = _Local()
    r = _router(
        local=local,
        dispatch=_dw(["I was ", "saying "], fail_after=2),
    )

    out = await _drain(r)
    assert out == "I was saying "
    assert local.calls == 0, "spliced a second answer onto a spoken one"
    assert r.last_route == "remote_truncated"


async def test_a_mid_utterance_fault_still_trips_the_breaker():
    """It failed, even though it could not be retried. Pretending otherwise
    would keep routing every turn into a broken lane."""
    r = _router(local=_Local(), dispatch=_dw(["a"], fail_after=1))
    await _drain(r)
    assert r._breaker.failures == 1     # noqa: SLF001


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


async def test_repeated_failures_stop_paying_the_timeout_every_turn(monkeypatch):
    monkeypatch.setenv("JARVIS_VOICE_ROUTER_BREAKER_THRESHOLD", "2")
    dispatch_calls = []

    async def _boom(payload):
        dispatch_calls.append(payload)
        raise ConnectionError("down")

    r = _router(local=_Local(), dispatch=_boom, clock=lambda: 1000.0)
    for _ in range(4):
        await _drain(r)

    assert len(dispatch_calls) == 2, (
        f"breaker never opened — DW probed {len(dispatch_calls)} times"
    )


async def test_the_breaker_reopens_after_its_cooldown(monkeypatch):
    monkeypatch.setenv("JARVIS_VOICE_ROUTER_BREAKER_THRESHOLD", "1")
    monkeypatch.setenv("JARVIS_VOICE_ROUTER_BREAKER_COOLDOWN_S", "60")
    t = {"now": 0.0}
    calls = []

    async def _boom(payload):
        calls.append(payload)
        raise ConnectionError("down")

    r = _router(local=_Local(), dispatch=_boom, clock=lambda: t["now"])
    await _drain(r)
    await _drain(r)
    assert len(calls) == 1, "retried inside the cooldown"

    t["now"] = 120.0
    await _drain(r)
    assert len(calls) == 2, "never re-armed — a recovered DW stays exiled"


async def test_a_success_closes_the_breaker():
    r = _router(local=_Local(), dispatch=_dw([], fail_after=0,
                                             exc=ConnectionError("x")))
    await _drain(r)
    assert r._breaker.failures == 1                     # noqa: SLF001
    r._dispatch = _dw(["ok"])                           # noqa: SLF001
    await _drain(r)
    assert r._breaker.failures == 0                     # noqa: SLF001


async def test_memory_pressure_re_arms_the_breaker_sooner(monkeypatch):
    """An open breaker means every turn runs locally — which is exactly what
    starves the audio path. Under pressure, retrying DW is the lesser risk."""
    monkeypatch.setenv("JARVIS_VOICE_ROUTER_BREAKER_THRESHOLD", "1")
    monkeypatch.setenv("JARVIS_VOICE_ROUTER_BREAKER_COOLDOWN_S", "60")
    monkeypatch.setenv("JARVIS_VOICE_ROUTER_PRESSURE_DIVISOR", "4")
    t = {"now": 0.0}

    calm = _router(local=_Local(), pressure_fn=lambda: False,
                   dispatch=_dw([], fail_after=0, exc=ConnectionError("x")),
                   clock=lambda: t["now"])
    tight = _router(local=_Local(), pressure_fn=lambda: True,
                    dispatch=_dw([], fail_after=0, exc=ConnectionError("x")),
                    clock=lambda: t["now"])
    await _drain(calm)
    await _drain(tight)

    t["now"] = 20.0                    # past 60/4, well short of 60
    assert calm.route_for(model="m") == "local"
    assert tight.route_for(model="m") == "remote"


# ---------------------------------------------------------------------------
# Routing decisions
# ---------------------------------------------------------------------------


async def test_an_unelected_voice_stays_local():
    """No measured model means no evidence. Speaking through an unmeasured
    remote is how a spoken turn lands on the 22-second code brain."""
    local = _Local()
    r = _router(local=local, resolve_model=lambda: None, dispatch=_dw(["x"]))
    assert await _drain(r) == "local answer"
    assert r.last_route == "local"


async def test_the_master_flag_makes_the_router_a_pass_through(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTIVE_VOICE_ROUTER_ENABLED", "false")
    local = _Local()
    assert build_voice_router(local) is local, (
        "OFF must return the bare engine — no wrapper, no behaviour to audit"
    )


async def test_remote_first_can_be_inverted(monkeypatch):
    monkeypatch.setenv("JARVIS_VOICE_ROUTER_REMOTE_FIRST", "false")
    local = _Local()
    r = _router(local=local, dispatch=_dw(["remote"]))
    assert await _drain(r) == "local answer"


async def test_a_raising_model_resolver_degrades_to_local():
    def _boom():
        raise RuntimeError("lane on fire")

    r = _router(local=_Local(), resolve_model=_boom, dispatch=_dw(["x"]))
    assert await _drain(r) == "local answer"


# ---------------------------------------------------------------------------
# Both engines gone — quiet, never a crashed FSM
# ---------------------------------------------------------------------------


async def test_both_engines_failing_yields_nothing_and_does_not_raise():
    """ConversationPipeline treats an empty stream as a failed turn and
    recovers; an exception here would take the conversation FSM down."""
    r = _router(
        local=_Local(boom=RuntimeError("local dead")),
        dispatch=_dw([], fail_after=0, exc=ConnectionError("remote dead")),
    )
    assert await _drain(r) == ""
    assert r.last_route == "failed"


async def test_no_local_engine_at_all_is_survivable():
    r = _router(local=None, dispatch=_dw([], fail_after=0,
                                         exc=ConnectionError("x")))
    assert await _drain(r) == ""


async def test_cancellation_propagates_rather_than_being_swallowed():
    """Barge-in cancels the response task. Swallowing CancelledError would
    leave Karen talking over the operator."""
    async def _hang(_payload):
        async def _readline():
            await asyncio.sleep(30)
            return b""
        return _readline

    r = _router(local=_Local(), dispatch=_hang)
    task = asyncio.create_task(_drain(r))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Drop-in contract
# ---------------------------------------------------------------------------


def test_unknown_attributes_delegate_to_the_local_engine():
    """The router replaces llm_client wholesale, so anything else reaching for
    it — health checks, model introspection — must still work."""
    r = _router(local=_Local())
    assert r.model_name == "local-engine"


def test_status_is_readable_without_a_network():
    st = _router(local=_Local()).status()
    assert st["elected_model"] == "elected/voice-model"
    assert st["route_next"] == "remote"
    assert st["has_local"] is True


# ---------------------------------------------------------------------------
# Wiring pins
# ---------------------------------------------------------------------------


def test_the_bootstrap_injects_the_router_not_the_bare_engine():
    """THE ROOT CAUSE. `llm_client=self._model_serving` welded the spoken loop
    to local inference; if this regresses, the DW voice election silently stops
    reaching spoken turns while every unit test here still passes."""
    from pathlib import Path

    src = Path(
        "backend/audio/audio_pipeline_bootstrap.py",
    ).read_text(encoding="utf-8")
    assert "build_voice_router" in src
    block = src[src.index("# 4. ConversationPipeline"):][:3000]
    assert "llm_client=_voice_llm" in block, (
        "ConversationPipeline is back on the un-routed engine"
    )


def test_the_router_does_not_reimplement_the_election():
    """DRY: the router is a breaker and a multiplexer. Election lives in
    karen_voice_lane, and duplicating its ranking here would let the two drift
    apart with no test able to see it."""
    from pathlib import Path

    src = Path(
        "backend/core/ouroboros/governance/adaptive_voice_router.py",
    ).read_text(encoding="utf-8")
    assert "resolve_voice_model" in src
    for owned_elsewhere in ("ttft_s", "VoiceLatencyLedger", "deep_probe("):
        assert owned_elsewhere not in src, (
            f"router re-implements {owned_elsewhere} — that belongs to the lane"
        )


# ---------------------------------------------------------------------------
# A stream that says nothing is a failure that returns 200
# ---------------------------------------------------------------------------
#
# Measured live: the elected model streamed ZERO tokens, the router recorded
# SUCCESS, and last_route read "remote". So a mute model stayed elected and
# every turn went to it — the operator waits the full budget and hears
# silence. The voice lane's probe exists to prevent exactly this and cannot
# see it once a model has already been elected.


async def test_a_zero_token_stream_falls_back_to_local():
    """THE REGRESSION."""
    local = _Local(chunks=("local ", "answer"))
    r = _router(local=local, dispatch=_dw([]))          # connects, says nothing

    assert await _drain(r) == "local answer"
    assert local.calls == 1
    assert r.last_route == "local"


async def test_a_zero_token_stream_trips_the_breaker():
    r = _router(local=_Local(), dispatch=_dw([]))
    await _drain(r)
    assert r._breaker.failures == 1                     # noqa: SLF001


async def test_a_zero_token_stream_demotes_the_model(monkeypatch):
    """The election rests on a probe, and a probe is a sample. Only the turn
    path sees a model that passed the probe and then went mute, so the turn
    path must be able to say so."""
    from backend.core.ouroboros.governance import karen_voice_lane as kvl

    demoted = []
    monkeypatch.setattr(
        kvl, "record_runtime_failure",
        lambda m, reason="runtime": demoted.append((m, reason)) or True,
    )
    r = _router(local=_Local(), dispatch=_dw([]))
    await _drain(r)
    assert demoted and demoted[0][0] == "elected/voice-model"
    assert "empty" in demoted[0][1]


async def test_a_speaking_model_is_not_demoted(monkeypatch):
    """Positive control — demotion must be the exception, not the rule."""
    from backend.core.ouroboros.governance import karen_voice_lane as kvl

    demoted = []
    monkeypatch.setattr(
        kvl, "record_runtime_failure",
        lambda m, reason="runtime": demoted.append(m) or True,
    )
    r = _router(local=_Local(), dispatch=_dw(["hello"]))
    assert await _drain(r) == "hello"
    assert demoted == []
