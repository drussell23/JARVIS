"""Slice 142 — The Discord Observability Bridge.

Turns Discord into a live window into the organism: an in-process subscriber to
the existing StreamEventBroker (~57 event types, ~40 producers) that routes each
event to a per-channel webhook (#ops / #subagents / #cost-safety / #commits /
#heartbeat), BATCHED + THROTTLED so it never trips Discord's webhook rate limit.

Gated JARVIS_DISCORD_BRIDGE_ENABLED default-FALSE; webhook URLs from env
(JARVIS_DISCORD_WEBHOOK_<CHANNEL>); async + fail-soft (a dead webhook never
perturbs the soak). The poster + event source are injectable → no network in tests.
"""
from __future__ import annotations

import asyncio
import os
import unittest

from backend.core.ouroboros.governance import discord_observability_bridge as DB


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _ev(event_type, op_id="op1", **payload):
    return DB.BridgeEvent(event_type=event_type, op_id=op_id, payload=payload)


class TestGate(unittest.TestCase):
    def setUp(self):
        os.environ.pop("JARVIS_DISCORD_BRIDGE_ENABLED", None)

    def test_default_false(self):
        self.assertFalse(DB.discord_bridge_enabled())


class TestRouting(unittest.TestCase):
    def test_channel_for_known_types(self):
        self.assertEqual(DB.channel_for("task_completed"), "ops")
        self.assertEqual(DB.channel_for("execution_graph_progress"), "subagents")
        self.assertEqual(DB.channel_for("budget_action_taken"), "cost_safety")
        self.assertEqual(DB.channel_for("circuit_breaker_tripped"), "cost_safety")
        self.assertEqual(DB.channel_for("commit_authority_decision_recorded"), "commits")
        self.assertEqual(DB.channel_for("posture_changed"), "heartbeat")

    def test_channel_for_unknown_is_none(self):
        self.assertIsNone(DB.channel_for("dashboard_rendered"))  # noise → dropped
        self.assertIsNone(DB.channel_for("heartbeat"))           # SSE keepalive → dropped

    def test_webhook_url_from_env(self):
        os.environ["JARVIS_DISCORD_WEBHOOK_OPS"] = "https://hook/ops"
        try:
            self.assertEqual(DB.webhook_url_for("ops"), "https://hook/ops")
            self.assertIsNone(DB.webhook_url_for("nonexistent"))
        finally:
            os.environ.pop("JARVIS_DISCORD_WEBHOOK_OPS", None)


class TestFormat(unittest.TestCase):
    def test_format_compact_and_bounded(self):
        evs = [_ev("task_started", op_id=f"op{i}") for i in range(200)]
        msg = DB.format_events("ops", evs)
        self.assertIn("ops", msg)
        self.assertIn("task_started", msg)
        self.assertLessEqual(len(msg), 2000)  # Discord hard cap


class TestBatchAndFlush(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_DISCORD_BRIDGE_ENABLED"] = "1"
        os.environ["JARVIS_DISCORD_WEBHOOK_OPS"] = "https://hook/ops"
        os.environ["JARVIS_DISCORD_WEBHOOK_COMMITS"] = "https://hook/commits"

    def tearDown(self):
        for k in ("JARVIS_DISCORD_BRIDGE_ENABLED", "JARVIS_DISCORD_WEBHOOK_OPS",
                  "JARVIS_DISCORD_WEBHOOK_COMMITS"):
            os.environ.pop(k, None)

    def test_ingest_buckets_by_channel_then_flush_posts_once_per_channel(self):
        posts = []
        async def _poster(url, content):
            posts.append((url, content))
            return 204
        b = DB.DiscordBridge(min_post_interval_s=0)
        b.ingest(_ev("task_started"))
        b.ingest(_ev("task_completed"))
        b.ingest(_ev("commit_authority_decision_recorded"))
        _run(b.flush(poster=_poster))
        urls = sorted(u for u, _ in posts)
        self.assertEqual(urls, ["https://hook/commits", "https://hook/ops"])  # one each

    def test_unknown_and_unconfigured_dropped(self):
        posts = []
        async def _poster(url, content):
            posts.append(url); return 204
        b = DB.DiscordBridge(min_post_interval_s=0)
        b.ingest(_ev("dashboard_rendered"))                 # unknown → no channel
        b.ingest(_ev("execution_graph_progress"))           # subagents → no webhook configured
        _run(b.flush(poster=_poster))
        self.assertEqual(posts, [])

    def test_throttle_suppresses_rapid_reflush(self):
        posts = []
        async def _poster(url, content):
            posts.append(url); return 204
        b = DB.DiscordBridge(min_post_interval_s=10_000)
        b.ingest(_ev("task_started")); _run(b.flush(poster=_poster))
        b.ingest(_ev("task_completed")); _run(b.flush(poster=_poster))  # within interval
        self.assertEqual(len(posts), 1)  # second flush throttled

    def test_dispatch_failsoft(self):
        async def _boom(url, content):
            raise RuntimeError("discord down")
        b = DB.DiscordBridge(min_post_interval_s=0)
        b.ingest(_ev("task_started"))
        _run(b.flush(poster=_boom))  # must not raise


class TestRunLoop(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_DISCORD_BRIDGE_ENABLED"] = "1"
        os.environ["JARVIS_DISCORD_WEBHOOK_OPS"] = "https://hook/ops"

    def tearDown(self):
        for k in ("JARVIS_DISCORD_BRIDGE_ENABLED", "JARVIS_DISCORD_WEBHOOK_OPS"):
            os.environ.pop(k, None)

    def test_run_consumes_source_and_posts(self):
        posts = []
        async def _poster(url, content):
            posts.append(url); return 204
        async def go():
            q: asyncio.Queue = asyncio.Queue()
            await q.put(_ev("task_started"))
            await q.put(_ev("task_completed"))
            b = DB.DiscordBridge(min_post_interval_s=0, flush_interval_s=0.02)
            stop = asyncio.Event()
            task = asyncio.ensure_future(b.run(source=q, poster=_poster, stop=stop))
            await asyncio.sleep(0.1)
            stop.set()
            await task
            self.assertTrue(posts)
        _run(go())


if __name__ == "__main__":
    unittest.main()
