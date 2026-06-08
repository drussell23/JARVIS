"""Slice 158 — Robust Channel Dispatch.

The Discord account-limit proved DMs are a fragile governance surface. This routes
APPROVAL_REQUIRED embeds + [APPROVE]/[REJECT]/[STEER] to the #governance-gates text
channel instead of operator DMs. The interaction.user.id authorization guard stays
strictly intact so unauthorized clicks in the shared channel are dropped.

Testable cores (the discord.py daemon is not unit-testable):
  - gates_channel_name / gates_channel_id env resolution.
  - pick_gates_channel: resolves the target channel by id (override) or by name,
    from the bot's visible channels.
"""
from __future__ import annotations

import os
import unittest

from backend.core.ouroboros.governance import discord_gateway as DG


class _FakeChannel:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name


class TestGatesChannelEnv(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("JARVIS_DISCORD_GATES_CHANNEL", None)
        os.environ.pop("JARVIS_DISCORD_GATES_CHANNEL_ID", None)

    def test_default_channel_name(self):
        os.environ.pop("JARVIS_DISCORD_GATES_CHANNEL", None)
        self.assertEqual(DG.gates_channel_name(), "governance-gates")

    def test_channel_name_override(self):
        os.environ["JARVIS_DISCORD_GATES_CHANNEL"] = "ops-approvals"
        self.assertEqual(DG.gates_channel_name(), "ops-approvals")

    def test_channel_id_optional(self):
        os.environ.pop("JARVIS_DISCORD_GATES_CHANNEL_ID", None)
        self.assertEqual(DG.gates_channel_id(), "")
        os.environ["JARVIS_DISCORD_GATES_CHANNEL_ID"] = "12345"
        self.assertEqual(DG.gates_channel_id(), "12345")


class TestPickGatesChannel(unittest.TestCase):
    def setUp(self):
        self.channels = [
            _FakeChannel(111, "general"),
            _FakeChannel(222, "governance-gates"),
            _FakeChannel(333, "cost-safety"),
        ]

    def test_pick_by_explicit_id(self):
        ch = DG.pick_gates_channel(self.channels, channel_id="333", channel_name="governance-gates")
        self.assertEqual(ch.id, 333)  # id override wins over name

    def test_pick_by_name_when_no_id(self):
        ch = DG.pick_gates_channel(self.channels, channel_id="", channel_name="governance-gates")
        self.assertEqual(ch.id, 222)

    def test_returns_none_when_absent(self):
        ch = DG.pick_gates_channel(self.channels, channel_id="", channel_name="does-not-exist")
        self.assertIsNone(ch)

    def test_id_miss_falls_back_to_name(self):
        ch = DG.pick_gates_channel(self.channels, channel_id="999", channel_name="governance-gates")
        self.assertEqual(ch.id, 222)  # bad id → resolve by name instead of failing


if __name__ == "__main__":
    unittest.main()
