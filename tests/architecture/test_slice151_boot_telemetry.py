"""Slice 151 — instant [boot] telemetry.

Verify-first killed the "boot sequencer" (the cold-boot 27s is a wall-clock artifact
during a busy-but-non-blocking boot; the real contributor is the Oracle worker dying
on missing lean-image deps — async churn, not a loop block). But the operator's
underlying Stage-1 goal is real: the Discord bridge should post a visible [boot]
ping the instant it arms, so the organism is observable within seconds of startup
regardless of background warm-up. This is that — small, safe, fail-soft.
"""
from __future__ import annotations

import asyncio
import os
import unittest

from backend.core.ouroboros.governance import discord_observability_bridge as DB


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestBootPing(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_DISCORD_WEBHOOK_HEARTBEAT"] = "https://hook/heartbeat"

    def tearDown(self):
        os.environ.pop("JARVIS_DISCORD_WEBHOOK_HEARTBEAT", None)

    def test_post_boot_posts_to_heartbeat_immediately(self):
        posts = []
        async def _poster(url, content):
            posts.append((url, content))
            return 204
        b = DB.DiscordBridge(min_post_interval_s=0)
        ok = _run(b.post_boot("organism igniting", poster=_poster))
        self.assertTrue(ok)
        self.assertEqual(posts[0][0], "https://hook/heartbeat")
        self.assertIn("boot", posts[0][1].lower())
        self.assertIn("organism igniting", posts[0][1])

    def test_post_boot_failsoft_when_no_heartbeat_webhook(self):
        os.environ.pop("JARVIS_DISCORD_WEBHOOK_HEARTBEAT", None)
        async def _poster(url, content):
            return 204
        b = DB.DiscordBridge(min_post_interval_s=0)
        self.assertFalse(_run(b.post_boot("x", poster=_poster)))  # no webhook → no-op, no raise

    def test_post_boot_failsoft_on_poster_error(self):
        async def _boom(url, content):
            raise RuntimeError("discord down")
        b = DB.DiscordBridge(min_post_interval_s=0)
        # must not raise (a dead webhook never blocks boot)
        self.assertFalse(_run(b.post_boot("x", poster=_boom)))


if __name__ == "__main__":
    unittest.main()
