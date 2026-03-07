"""Tests for PolicyContext typed dataclass."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest


class TestPolicyContext:
    def test_frozen(self):
        from core.contracts.policy_context import PolicyContext
        ctx = PolicyContext(
            tier=1, score=90, message_id="msg-1",
            sender_domain="example.com", is_reply=False,
            has_attachment=False, label_ids=("INBOX",),
            cycle_id="cycle-1", fencing_token=1,
            config_version="v1",
        )
        with pytest.raises(AttributeError):
            ctx.tier = 2  # type: ignore[misc]

    def test_all_fields_accessible(self):
        from core.contracts.policy_context import PolicyContext
        ctx = PolicyContext(
            tier=2, score=75, message_id="msg-2",
            sender_domain="test.com", is_reply=True,
            has_attachment=True, label_ids=("INBOX", "IMPORTANT"),
            cycle_id="cycle-2", fencing_token=5,
            config_version="v2",
        )
        assert ctx.tier == 2
        assert ctx.score == 75
        assert ctx.message_id == "msg-2"
        assert ctx.sender_domain == "test.com"
        assert ctx.is_reply is True
        assert ctx.has_attachment is True
        assert ctx.label_ids == ("INBOX", "IMPORTANT")
        assert ctx.cycle_id == "cycle-2"
        assert ctx.fencing_token == 5
        assert ctx.config_version == "v2"
