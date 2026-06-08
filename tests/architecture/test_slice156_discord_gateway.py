"""Slice 156 — The Sovereign Bidirectional Gateway (interactive Discord control).

The webhook bridge (142-145) is a read-only megaphone. This adds remote ARBITRATION:
[APPROVE]/[REJECT]/[STEER] buttons that resolve the SAME approval rendezvous the
local TUI does — composing the existing CLIApprovalProvider (per-op asyncio.Event)
and ConversationBridge (TUI→FSM input spine). Every interaction is authorized
against DISCORD_OPERATOR_ID; an unauthorized click is dropped + logged REFUSED_SAFETY.

The command router is pure logic (no discord.py) → fully unit-tested here; the
discord.py gateway daemon is a thin adapter around it (needs a live bot token).
"""
from __future__ import annotations

import asyncio
import os
import unittest

from backend.core.ouroboros.governance import discord_gateway as DG


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeApprovalProvider:
    def __init__(self):
        self.approved = []
        self.rejected = []
    async def approve(self, request_id, approver):
        self.approved.append((request_id, approver)); return {"ok": True}
    async def reject(self, request_id, approver, reason=""):
        self.rejected.append((request_id, approver, reason)); return {"ok": True}


class _FakeBridge:
    def __init__(self):
        self.notes = []
    def note_tui_user(self, text):
        self.notes.append(text)


class TestGate(unittest.TestCase):
    def setUp(self):
        os.environ.pop("JARVIS_DISCORD_GATEWAY_ENABLED", None)

    def test_default_false(self):
        self.assertFalse(DG.discord_gateway_enabled())


class TestAuthorization(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("DISCORD_OPERATOR_ID", None)

    def test_authorized_matches_env(self):
        os.environ["DISCORD_OPERATOR_ID"] = "12345"
        self.assertTrue(DG.is_authorized_operator("12345"))
        self.assertTrue(DG.is_authorized_operator(12345))  # int coerced

    def test_unauthorized_mismatch(self):
        os.environ["DISCORD_OPERATOR_ID"] = "12345"
        self.assertFalse(DG.is_authorized_operator("99999"))

    def test_fail_closed_when_unset(self):
        os.environ.pop("DISCORD_OPERATOR_ID", None)
        self.assertFalse(DG.is_authorized_operator("12345"))  # no config → deny ALL


class TestCommandRouter(unittest.TestCase):
    def setUp(self):
        os.environ["DISCORD_OPERATOR_ID"] = "op-1"
        self.provider = _FakeApprovalProvider()
        self.bridge = _FakeBridge()
        self.refusals = []
        self.router = DG.GatewayCommandRouter(
            approval_provider=self.provider,
            conversation_bridge=self.bridge,
            on_refused=lambda **k: self.refusals.append(k),
        )

    def tearDown(self):
        os.environ.pop("DISCORD_OPERATOR_ID", None)

    def test_approve_resolves_via_provider(self):
        res = _run(self.router.handle("approve", "op-42", "op-1"))
        self.assertTrue(res["ok"])
        self.assertEqual(self.provider.approved[0][0], "op-42")
        self.assertIn("discord", self.provider.approved[0][1])

    def test_reject_resolves_via_provider(self):
        _run(self.router.handle("reject", "op-42", "op-1", text="bad diff"))
        self.assertEqual(self.provider.rejected[0][0], "op-42")
        self.assertIn("bad diff", self.provider.rejected[0][2])

    def test_steer_feeds_bridge_and_unblocks(self):
        _run(self.router.handle("steer", "op-42", "op-1", text="prefer async"))
        self.assertIn("prefer async", self.bridge.notes[0])     # constraint → FSM input spine
        self.assertTrue(self.provider.rejected)                  # current op unblocked

    def test_unauthorized_click_dropped_and_refused(self):
        res = _run(self.router.handle("approve", "op-42", "intruder"))
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("refused"))
        self.assertEqual(self.provider.approved, [])             # NO action taken
        self.assertTrue(self.refusals)                           # REFUSED_SAFETY logged
        self.assertEqual(self.refusals[0]["user_id"], "intruder")

    def test_unknown_action_noop(self):
        res = _run(self.router.handle("nuke", "op-42", "op-1"))
        self.assertFalse(res["ok"])
        self.assertEqual(self.provider.approved, [])
        self.assertEqual(self.provider.rejected, [])


if __name__ == "__main__":
    unittest.main()
