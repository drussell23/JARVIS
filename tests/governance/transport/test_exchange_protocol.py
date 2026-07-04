# -*- coding: utf-8 -*-
"""Exchange protocol (pure) + Brain echo responder (in-proc broker) spine."""
from __future__ import annotations

import asyncio

from backend.core.ouroboros.governance.transport import exchange_protocol as xp


OP = xp.EXCHANGE_OP_PREFIX + "m2b-123-0"


# --------------------------------------------------------------------------- #
# respond(): the Brain-side pure decision table.
# --------------------------------------------------------------------------- #
def test_plain_exchange_event_is_echoed():
    out = xp.respond(xp.EXCHANGE_EVENT_TYPE, OP, {"seq": 0, "tag": "m2b"})
    assert out == [(xp.EXCHANGE_EVENT_TYPE, OP,
                    {xp.K_ECHO_OF_OP: OP, xp.K_ORIGIN: xp.ORIGIN_ECHO})]


def test_publish_cmd_yields_brain_origin_nonce_event():
    out = xp.respond(xp.EXCHANGE_EVENT_TYPE, OP, xp.make_publish_cmd("n-42"))
    assert out == [(xp.EXCHANGE_EVENT_TYPE, OP,
                    {xp.K_NONCE: "n-42", xp.K_ORIGIN: xp.ORIGIN_BRAIN})]


def test_own_outputs_are_never_reacted_to():
    echo_payload = {xp.K_ECHO_OF_OP: OP, xp.K_ORIGIN: xp.ORIGIN_ECHO}
    nonce_payload = {xp.K_NONCE: "n-1", xp.K_ORIGIN: xp.ORIGIN_BRAIN}
    assert xp.respond(xp.EXCHANGE_EVENT_TYPE, OP, echo_payload) == []
    assert xp.respond(xp.EXCHANGE_EVENT_TYPE, OP, nonce_payload) == []


def test_non_exchange_ops_and_types_are_ignored():
    # The organism's real task_started traffic (no exchange prefix) is sacred.
    assert xp.respond(xp.EXCHANGE_EVENT_TYPE, "op-019f-real", {"tag": "x"}) == []
    assert xp.respond("task_completed", OP, {"tag": "m2b"}) == []
    assert xp.respond(xp.EXCHANGE_EVENT_TYPE, OP, {"no": "tag"}) == []


def test_cmd_without_nonce_is_dropped():
    assert xp.respond(xp.EXCHANGE_EVENT_TYPE, OP, {xp.K_CMD: xp.CMD_PUBLISH}) == []


# --------------------------------------------------------------------------- #
# parse_observed(): the Mac-side classification.
# --------------------------------------------------------------------------- #
def test_parse_observed_echo_and_nonce_and_noise():
    assert xp.parse_observed(
        xp.EXCHANGE_EVENT_TYPE, OP,
        {xp.K_ECHO_OF_OP: OP, xp.K_ORIGIN: xp.ORIGIN_ECHO}) == ("echo", OP)
    assert xp.parse_observed(
        xp.EXCHANGE_EVENT_TYPE, OP,
        {xp.K_NONCE: "n-7", xp.K_ORIGIN: xp.ORIGIN_BRAIN}) == ("nonce", "n-7")
    assert xp.parse_observed(xp.EXCHANGE_EVENT_TYPE, OP, {"tag": "m2b"}) is None
    assert xp.parse_observed(xp.EXCHANGE_EVENT_TYPE, "op-019f-real",
                             {xp.K_NONCE: "n", xp.K_ORIGIN: "brain"}) is None


# --------------------------------------------------------------------------- #
# run_responder(): wired against a REAL in-proc StreamEventBroker.
# --------------------------------------------------------------------------- #
def test_responder_echoes_and_answers_cmds_on_real_broker():
    import importlib.util
    import sys
    from pathlib import Path

    from backend.core.ouroboros.governance.ide_observability_stream import (
        StreamEventBroker,
    )

    script = Path(__file__).resolve().parents[3] / "scripts" / "brain_bus_echo_server.py"
    spec = importlib.util.spec_from_file_location("brain_bus_echo_server", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("brain_bus_echo_server", mod)
    spec.loader.exec_module(mod)

    async def scenario():
        broker = StreamEventBroker()
        touches = []
        task = asyncio.ensure_future(
            mod.run_responder(broker, touch_fn=lambda: touches.append(1)))
        await asyncio.sleep(0.05)  # subscriber armed

        broker.publish(xp.EXCHANGE_EVENT_TYPE, OP, {"seq": 0, "tag": "m2b"})
        cmd_op = xp.EXCHANGE_OP_PREFIX + "b2m-123-0"
        broker.publish(xp.EXCHANGE_EVENT_TYPE, cmd_op, xp.make_publish_cmd("n-9"))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return broker, touches

    broker, touches = asyncio.get_event_loop().run_until_complete(scenario())
    # History carries: original + echo + cmd + nonce response = 4 events.
    assert broker.published_count == 4
    assert touches, "liveness must be touched on WS-peer activity"


def test_responder_does_not_echo_its_own_echo_no_storm():
    import brain_bus_echo_server as mod  # loaded by the previous test

    from backend.core.ouroboros.governance.ide_observability_stream import (
        StreamEventBroker,
    )

    async def scenario():
        broker = StreamEventBroker()
        task = asyncio.ensure_future(
            mod.run_responder(broker, touch_fn=lambda: None))
        await asyncio.sleep(0.05)
        broker.publish(xp.EXCHANGE_EVENT_TYPE, OP, {"seq": 0, "tag": "m2b"})
        await asyncio.sleep(0.3)  # long enough for any echo-of-echo storm
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return broker

    broker = asyncio.get_event_loop().run_until_complete(scenario())
    assert broker.published_count == 2, "exactly original + one echo -- no storm"
