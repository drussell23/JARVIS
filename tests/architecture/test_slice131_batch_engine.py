"""Slice 131 Phase 2b — the Asynchronous Claude Batch Engine.

Non-urgent (BACKGROUND / SPECULATIVE) ops don't need a real-time response, so
they should ride the Anthropic Message Batches endpoint (flat 50% discount)
instead of the standard completions path. This engine packages the prompt into
the batch request, dispatches it, polls to completion (AWAITING_BATCH), retrieves
the result, and returns it as if real-time — with a hard FALLBACK invariant:
any 4xx/5xx/unavailable/timeout → seamlessly fall back to the real-time path so
the op is never starved.

Gated ``JARVIS_BATCH_ROUTING_ENABLED`` default-FALSE; the (sync) Anthropic batch
client is injectable so the engine is tested without network.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import unittest

from backend.core.ouroboros.governance import batch_engine as BE


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── fake Anthropic batch client (sync, matches messages.batches.*) ──────────
class _Res:
    def __init__(self, custom_id, type_, message=None, error=None):
        self.custom_id = custom_id
        self.result = type("R", (), {"type": type_, "message": message, "error": error})()


class _Batch:
    def __init__(self, id_, status):
        self.id = id_
        self.processing_status = status


class _FakeBatches:
    def __init__(self, *, ended_after=1, fail_create=False, results=None):
        self.ended_after = ended_after
        self.fail_create = fail_create
        self._results = results or []
        self.polls = 0
        self.created_requests = None

    def create(self, requests=None):
        if self.fail_create:
            raise RuntimeError("HTTP 429 batch unavailable")
        self.created_requests = requests
        return _Batch("batch_xyz", "in_progress")

    def retrieve(self, batch_id):
        self.polls += 1
        return _Batch(batch_id, "ended" if self.polls >= self.ended_after else "in_progress")

    def results(self, batch_id):
        return iter(self._results)


def _client(batches):
    return type("C", (), {"messages": type("M", (), {"batches": batches})()})()


async def _fallback_factory(flag):
    async def _fb():
        flag["called"] = True
        return "REALTIME_RESULT"
    return _fb


class TestGateAndEligibility(unittest.TestCase):
    def setUp(self):
        os.environ.pop("JARVIS_BATCH_ROUTING_ENABLED", None)

    def test_default_false(self):
        self.assertFalse(BE.batch_routing_enabled())

    def test_eligibility(self):
        self.assertTrue(BE.is_batch_eligible("background"))
        self.assertTrue(BE.is_batch_eligible("speculative"))
        self.assertFalse(BE.is_batch_eligible("immediate"))
        self.assertFalse(BE.is_batch_eligible("standard"))
        self.assertFalse(BE.is_batch_eligible(""))

    def test_pack_request_shape(self):
        req = BE.pack_batch_request("op-1", "do the thing", "claude-x", max_tokens=2048,
                                    system="be terse")
        self.assertEqual(req["custom_id"], "op-1")
        self.assertEqual(req["params"]["model"], "claude-x")
        self.assertEqual(req["params"]["max_tokens"], 2048)
        self.assertEqual(req["params"]["system"], "be terse")
        self.assertEqual(req["params"]["messages"][0]["role"], "user")
        self.assertIn("do the thing", req["params"]["messages"][0]["content"])


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_BATCH_ROUTING_ENABLED"] = "1"
        os.environ["JARVIS_BATCH_POLL_INTERVAL_S"] = "0.01"
        os.environ["JARVIS_BATCH_MAX_WAIT_S"] = "2"

    def tearDown(self):
        for k in ("JARVIS_BATCH_ROUTING_ENABLED", "JARVIS_BATCH_POLL_INTERVAL_S",
                  "JARVIS_BATCH_MAX_WAIT_S"):
            os.environ.pop(k, None)

    def _engine(self, batches):
        return BE.ClaudeBatchEngine(client=_client(batches))

    def test_happy_path_returns_batched_result(self):
        results = [_Res("op-1", "succeeded", message="BATCHED_MSG")]
        eng = self._engine(_FakeBatches(ended_after=2, results=results))
        flag = {"called": False}
        out = _run(eng.generate_or_fallback(
            prompt="p", model="m", route="background", custom_id="op-1",
            fallback=_run(_fallback_factory(flag)),
        ))
        self.assertEqual(out.result, "BATCHED_MSG")
        self.assertEqual(out.state, BE.BatchState.COMPLETED)
        self.assertFalse(flag["called"])  # real-time NOT hit → 50% saved

    def test_disabled_falls_back(self):
        os.environ["JARVIS_BATCH_ROUTING_ENABLED"] = "0"
        eng = self._engine(_FakeBatches())
        flag = {"called": False}
        out = _run(eng.generate_or_fallback(
            prompt="p", model="m", route="background",
            fallback=_run(_fallback_factory(flag)),
        ))
        self.assertEqual(out.result, "REALTIME_RESULT")
        self.assertTrue(flag["called"])
        self.assertEqual(out.state, BE.BatchState.FELL_BACK_DISABLED)

    def test_ineligible_route_falls_back(self):
        eng = self._engine(_FakeBatches())
        flag = {"called": False}
        out = _run(eng.generate_or_fallback(
            prompt="p", model="m", route="immediate",
            fallback=_run(_fallback_factory(flag)),
        ))
        self.assertTrue(flag["called"])
        self.assertEqual(out.state, BE.BatchState.FELL_BACK_INELIGIBLE)

    def test_create_4xx_5xx_falls_back(self):
        eng = self._engine(_FakeBatches(fail_create=True))
        flag = {"called": False}
        out = _run(eng.generate_or_fallback(
            prompt="p", model="m", route="speculative",
            fallback=_run(_fallback_factory(flag)),
        ))
        self.assertEqual(out.result, "REALTIME_RESULT")
        self.assertTrue(flag["called"])
        self.assertEqual(out.state, BE.BatchState.FELL_BACK_FAULT)

    def test_poll_timeout_falls_back(self):
        os.environ["JARVIS_BATCH_MAX_WAIT_S"] = "0.05"
        eng = self._engine(_FakeBatches(ended_after=9999))  # never ends in time
        flag = {"called": False}
        out = _run(eng.generate_or_fallback(
            prompt="p", model="m", route="background",
            fallback=_run(_fallback_factory(flag)),
        ))
        self.assertTrue(flag["called"])
        self.assertEqual(out.state, BE.BatchState.FELL_BACK_TIMEOUT)

    def test_errored_result_falls_back(self):
        results = [_Res("op-1", "errored", error="server_error")]
        eng = self._engine(_FakeBatches(ended_after=1, results=results))
        flag = {"called": False}
        out = _run(eng.generate_or_fallback(
            prompt="p", model="m", route="background", custom_id="op-1",
            fallback=_run(_fallback_factory(flag)),
        ))
        self.assertTrue(flag["called"])
        self.assertEqual(out.state, BE.BatchState.FELL_BACK_ERROR)


class TestNoHardcode(unittest.TestCase):
    def test_no_hardcoded_model(self):
        src = pathlib.Path(
            "backend/core/ouroboros/governance/batch_engine.py"
        ).read_text()
        for banned in ("claude-haiku", "claude-sonnet", "claude-opus", "claude-3"):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
