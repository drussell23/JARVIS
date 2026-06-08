"""Slice 157 — Live-Fire Interaction Matrix.

A GENUINE operation injected via the real EventChannel /webhook/generic ingress
flows through the real FSM, is elevated to APPROVAL_REQUIRED by the real risk-tier
floor, and the gateway DMs a real interactive view. No demo flags, no no-ops.

Testable cores (the discord.py daemon + live HTTP are not unit-testable):
  - build_livefire_payload: encodes the task into the webhook payload so
    _classify_event renders it as the op description.
  - summarize_pending: the embed must show the real op (description), not a bare op_id
    (fixes the list_pending 'description' vs gateway 'summary' field mismatch).
  - GatewayCommandRouter.on_decision: every authorized decision is broadcast (so a
    REJECT is logged to the governance-gates channel); unauthorized clicks are not.
"""
from __future__ import annotations

import asyncio
import os
import unittest

from backend.core.ouroboros.governance import discord_gateway as DG


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeProvider:
    def __init__(self):
        self.rejected = []
        self.approved = []
    async def approve(self, rid, approver):
        self.approved.append((rid, approver)); return {"ok": True}
    async def reject(self, rid, approver, reason=""):
        self.rejected.append((rid, approver, reason)); return {"ok": True}


class TestLiveFirePayload(unittest.TestCase):
    def test_encodes_task_in_type_field(self):
        p = DG.build_livefire_payload("add a docstring to gateway.py")
        # _classify_event uses payload['type'] as the event_type → op description
        self.assertEqual(p["type"], "add a docstring to gateway.py")

    def test_carries_signature_for_dedup(self):
        p = DG.build_livefire_payload("task")
        self.assertIn("signature", p)  # genuine signal needs a dedup signature


class TestSummarizePending(unittest.TestCase):
    def test_prefers_description(self):
        s = DG.summarize_pending({"op_id": "op-1", "description": "rewrite governance manifest"})
        self.assertIn("rewrite governance manifest", s)

    def test_includes_target_files_when_present(self):
        s = DG.summarize_pending({"op_id": "op-1", "description": "d", "target_files": ["backend/x.py"]})
        self.assertIn("backend/x.py", s)

    def test_falls_back_to_op_id(self):
        self.assertEqual(DG.summarize_pending({"op_id": "op-9"}), "op-9")


class TestDecisionBroadcast(unittest.TestCase):
    def setUp(self):
        os.environ["DISCORD_OPERATOR_ID"] = "op-1"
        self.provider = _FakeProvider()
        self.decisions = []
        self.router = DG.GatewayCommandRouter(
            approval_provider=self.provider,
            on_decision=lambda **k: self.decisions.append(k),
        )

    def tearDown(self):
        os.environ.pop("DISCORD_OPERATOR_ID", None)

    def test_reject_broadcasts_decision(self):
        _run(self.router.handle("reject", "op-42", "op-1", text="not safe"))
        self.assertEqual(len(self.decisions), 1)
        self.assertEqual(self.decisions[0]["action"], "reject")
        self.assertEqual(self.decisions[0]["op_id"], "op-42")
        self.assertEqual(self.decisions[0]["user_id"], "op-1")

    def test_approve_broadcasts_decision(self):
        _run(self.router.handle("approve", "op-42", "op-1"))
        self.assertEqual(self.decisions[0]["action"], "approve")

    def test_unauthorized_does_not_broadcast_decision(self):
        _run(self.router.handle("reject", "op-42", "intruder"))
        self.assertEqual(self.decisions, [])  # no decision — it was refused, not decided


class TestInjector(unittest.TestCase):
    def _load(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "trigger_live_approval", "scripts/trigger_live_approval.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_inject_posts_generic_webhook_with_task(self):
        import json as _json
        mod = self._load()
        captured = {}

        class _Resp:
            status = 200

        def fake_opener(req, timeout=10):
            captured["url"] = req.full_url
            captured["body"] = _json.loads(req.data.decode())
            return _Resp()

        status = mod.inject("rewrite governance manifest", opener=fake_opener)
        self.assertEqual(status, 200)
        self.assertIn("/webhook/generic", captured["url"])
        self.assertEqual(captured["body"]["type"], "rewrite governance manifest")


if __name__ == "__main__":
    unittest.main()
