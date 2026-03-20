"""Tests for the Langfuse observability transport.

All tests mock the Langfuse client entirely via a FakeLangfuse / FakeTrace
class hierarchy -- no real network calls or package installs required.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from backend.core.ouroboros.governance.comm_protocol import (
    CommMessage,
    CommProtocol,
    LogTransport,
    MessageType,
)
from backend.core.ouroboros.governance.comms.langfuse_transport import (
    LangfuseTransport,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSpan:
    """Records span() calls for assertions."""

    def __init__(self, *, name: str, metadata: Dict[str, Any], level: str = "DEFAULT"):
        self.name = name
        self.metadata = metadata
        self.level = level


class FakeTrace:
    """Records trace-level operations for assertions."""

    def __init__(self, *, name: str, id: str, metadata: Dict[str, Any], tags: List[str]):
        self.name = name
        self.id = id
        self.metadata = metadata
        self.tags = tags
        self.spans: List[FakeSpan] = []
        self.updates: List[Dict[str, Any]] = []

    def span(self, *, name: str, metadata: Dict[str, Any], level: str = "DEFAULT") -> FakeSpan:
        s = FakeSpan(name=name, metadata=metadata, level=level)
        self.spans.append(s)
        return s

    def update(self, *, metadata: Dict[str, Any]) -> None:
        self.updates.append(metadata)


class FakeLangfuse:
    """Minimal Langfuse stand-in that records traces and flush calls."""

    def __init__(self) -> None:
        self.traces: Dict[str, FakeTrace] = {}  # id -> trace
        self.flush_count: int = 0

    def trace(self, *, name: str, id: str, metadata: Dict[str, Any], tags: List[str]) -> FakeTrace:
        t = FakeTrace(name=name, id=id, metadata=metadata, tags=tags)
        self.traces[id] = t
        return t

    def flush(self) -> None:
        self.flush_count += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_msg(
    msg_type: MessageType,
    op_id: str = "op-test-lf",
    seq: int = 1,
    causal_parent_seq: Optional[int] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> CommMessage:
    return CommMessage(
        msg_type=msg_type,
        op_id=op_id,
        seq=seq,
        causal_parent_seq=causal_parent_seq,
        payload=payload or {},
    )


# ---------------------------------------------------------------------------
# Tests: No-op / disabled scenarios
# ---------------------------------------------------------------------------


class TestLangfuseTransportDisabled:
    """Transport must be a silent no-op when Langfuse is unavailable."""

    def test_noop_when_client_is_none(self):
        """Transport with no client reports is_active=False."""
        transport = LangfuseTransport(langfuse_client=None)
        # Explicitly passing None means _create_client is bypassed but
        # the __init__ conditional sets _langfuse to None only when
        # _create_client returns None.  We test with an explicit None.
        # Since we passed None, _create_client is NOT called (guarded by
        # ``is not None``), so _langfuse stays None.
        assert not transport.is_active

    @pytest.mark.asyncio
    async def test_send_noop_when_inactive(self):
        """send() returns immediately and never raises when inactive."""
        transport = LangfuseTransport(langfuse_client=None)
        msg = _make_msg(MessageType.INTENT, payload={"goal": "test"})
        # Must not raise
        await transport.send(msg)

    def test_create_client_returns_none_without_env_vars(self):
        """_create_client returns None when env vars are unset."""
        with patch.dict("os.environ", {}, clear=True):
            client = LangfuseTransport._create_client()
        assert client is None

    def test_create_client_returns_none_on_import_error(self):
        """_create_client returns None when langfuse package is missing."""
        env = {"LANGFUSE_PUBLIC_KEY": "pk-test", "LANGFUSE_SECRET_KEY": "sk-test"}
        with patch.dict("os.environ", env, clear=True):
            with patch("builtins.__import__", side_effect=_import_blocker):
                client = LangfuseTransport._create_client()
        assert client is None


def _import_blocker(name: str, *args: Any, **kwargs: Any) -> Any:
    """Simulate ImportError for the langfuse package."""
    if name == "langfuse":
        raise ImportError("simulated: langfuse not installed")
    return original_import(name, *args, **kwargs)


import builtins

original_import = builtins.__import__


# ---------------------------------------------------------------------------
# Tests: INTENT
# ---------------------------------------------------------------------------


class TestIntentTrace:
    """INTENT messages must create a new Langfuse trace."""

    @pytest.mark.asyncio
    async def test_intent_creates_trace(self):
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake)

        msg = _make_msg(
            MessageType.INTENT,
            op_id="op-intent-1",
            payload={
                "goal": "Refactor foo.py",
                "target_files": ["foo.py"],
                "risk_tier": "SAFE_AUTO",
                "blast_radius": 1,
            },
        )
        await transport.send(msg)

        assert "op-intent-1" in fake.traces
        trace = fake.traces["op-intent-1"]
        assert trace.name == "ouroboros-op"
        assert trace.id == "op-intent-1"
        assert trace.metadata["goal"] == "Refactor foo.py"
        assert trace.metadata["target_files"] == ["foo.py"]
        assert trace.metadata["risk_tier"] == "SAFE_AUTO"
        assert trace.metadata["blast_radius"] == 1
        assert "ouroboros" in trace.tags

    @pytest.mark.asyncio
    async def test_intent_with_missing_payload_fields(self):
        """INTENT with empty payload still creates a trace with defaults."""
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake)

        msg = _make_msg(MessageType.INTENT, op_id="op-empty", payload={})
        await transport.send(msg)

        assert "op-empty" in fake.traces
        trace = fake.traces["op-empty"]
        assert trace.metadata["goal"] == ""
        assert trace.metadata["target_files"] == []


# ---------------------------------------------------------------------------
# Tests: PLAN
# ---------------------------------------------------------------------------


class TestPlanSpan:
    """PLAN messages must create a span on the existing trace."""

    @pytest.mark.asyncio
    async def test_plan_creates_span(self):
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake)

        # First create the trace via INTENT
        await transport.send(
            _make_msg(MessageType.INTENT, op_id="op-plan-1", payload={"goal": "test"})
        )
        # Then send PLAN
        await transport.send(
            _make_msg(
                MessageType.PLAN,
                op_id="op-plan-1",
                seq=2,
                causal_parent_seq=1,
                payload={"source": "prime", "steps": ["s1", "s2"]},
            )
        )

        trace = fake.traces["op-plan-1"]
        assert len(trace.spans) == 1
        assert trace.spans[0].name == "plan-prime"
        assert trace.spans[0].metadata["steps"] == ["s1", "s2"]

    @pytest.mark.asyncio
    async def test_plan_without_trace_is_noop(self):
        """PLAN for an unknown op_id does not raise."""
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake)

        await transport.send(
            _make_msg(MessageType.PLAN, op_id="op-orphan", payload={"source": "x"})
        )
        # No trace was created, no spans anywhere
        assert len(fake.traces) == 0


# ---------------------------------------------------------------------------
# Tests: HEARTBEAT sampling
# ---------------------------------------------------------------------------


class TestHeartbeatSampling:
    """Heartbeats must be sampled to reduce Langfuse noise."""

    @pytest.mark.asyncio
    async def test_heartbeat_only_every_nth(self):
        """With sample_rate=3, only heartbeats 3, 6, 9, ... produce spans."""
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake, heartbeat_sample_rate=3)

        # Create trace
        await transport.send(
            _make_msg(MessageType.INTENT, op_id="op-hb", payload={"goal": "hb test"})
        )

        # Send 9 heartbeats
        for i in range(1, 10):
            await transport.send(
                _make_msg(
                    MessageType.HEARTBEAT,
                    op_id="op-hb",
                    seq=i + 1,
                    payload={"phase": "GENERATE", "progress_pct": i * 10.0},
                )
            )

        trace = fake.traces["op-hb"]
        # 9 heartbeats with sample_rate=3: heartbeats #3, #6, #9 recorded
        assert len(trace.spans) == 3
        for span in trace.spans:
            assert span.name == "heartbeat"

    @pytest.mark.asyncio
    async def test_heartbeat_without_trace_is_noop(self):
        """Heartbeat for unknown op_id does not raise."""
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake, heartbeat_sample_rate=1)

        await transport.send(
            _make_msg(MessageType.HEARTBEAT, op_id="op-ghost", payload={"phase": "x"})
        )
        assert len(fake.traces) == 0

    @pytest.mark.asyncio
    async def test_heartbeat_default_sample_rate(self):
        """Default sample rate is 5 (from env or fallback)."""
        transport = LangfuseTransport(langfuse_client=FakeLangfuse())
        assert transport._heartbeat_sample_rate == 5

    @pytest.mark.asyncio
    async def test_heartbeat_sample_rate_from_env(self):
        """Sample rate can be configured via environment variable."""
        with patch.dict("os.environ", {"LANGFUSE_HEARTBEAT_SAMPLE_RATE": "10"}):
            transport = LangfuseTransport(langfuse_client=FakeLangfuse())
        assert transport._heartbeat_sample_rate == 10


# ---------------------------------------------------------------------------
# Tests: DECISION
# ---------------------------------------------------------------------------


class TestDecisionSpan:
    """DECISION messages must create a span, update the trace, and flush."""

    @pytest.mark.asyncio
    async def test_decision_creates_span_and_flushes(self):
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake)

        await transport.send(
            _make_msg(MessageType.INTENT, op_id="op-dec", payload={"goal": "decide"})
        )
        await transport.send(
            _make_msg(
                MessageType.DECISION,
                op_id="op-dec",
                seq=2,
                payload={
                    "outcome": "applied",
                    "reason_code": "safe_auto",
                    "provider_used": "claude-api",
                },
            )
        )

        trace = fake.traces.get("op-dec")
        # Trace should still exist in FakeLangfuse.traces dict (we only clean
        # up the transport's internal _traces mapping)
        assert trace is not None
        assert len(trace.spans) == 1
        assert trace.spans[0].name == "decision-applied"
        assert trace.spans[0].level == "DEFAULT"
        # Trace was updated with outcome metadata
        assert len(trace.updates) == 1
        assert trace.updates[0]["outcome"] == "applied"
        assert trace.updates[0]["provider"] == "claude-api"
        # Flush was called
        assert fake.flush_count >= 1

    @pytest.mark.asyncio
    async def test_decision_blocked_gets_warning_level(self):
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake)

        await transport.send(
            _make_msg(MessageType.INTENT, op_id="op-block", payload={"goal": "block"})
        )
        await transport.send(
            _make_msg(
                MessageType.DECISION,
                op_id="op-block",
                seq=2,
                payload={"outcome": "blocked", "reason_code": "risk_too_high"},
            )
        )

        trace = fake.traces["op-block"]
        assert trace.spans[0].level == "WARNING"
        assert trace.spans[0].name == "decision-blocked"

    @pytest.mark.asyncio
    async def test_decision_escalated_gets_warning_level(self):
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake)

        await transport.send(
            _make_msg(MessageType.INTENT, op_id="op-esc", payload={"goal": "esc"})
        )
        await transport.send(
            _make_msg(
                MessageType.DECISION,
                op_id="op-esc",
                seq=2,
                payload={"outcome": "escalated", "reason_code": "needs_review"},
            )
        )

        trace = fake.traces["op-esc"]
        assert trace.spans[0].level == "WARNING"

    @pytest.mark.asyncio
    async def test_decision_cleans_up_op_state(self):
        """After DECISION the transport no longer tracks the op_id."""
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake)

        await transport.send(
            _make_msg(MessageType.INTENT, op_id="op-cleanup", payload={"goal": "x"})
        )
        assert "op-cleanup" in transport._traces

        await transport.send(
            _make_msg(
                MessageType.DECISION,
                op_id="op-cleanup",
                seq=2,
                payload={"outcome": "applied"},
            )
        )
        assert "op-cleanup" not in transport._traces
        assert "op-cleanup" not in transport._heartbeat_counters


# ---------------------------------------------------------------------------
# Tests: POSTMORTEM
# ---------------------------------------------------------------------------


class TestPostmortemSpan:
    """POSTMORTEM messages must create an ERROR span and flush."""

    @pytest.mark.asyncio
    async def test_postmortem_creates_error_span_and_flushes(self):
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake)

        await transport.send(
            _make_msg(MessageType.INTENT, op_id="op-pm", payload={"goal": "fail"})
        )
        await transport.send(
            _make_msg(
                MessageType.POSTMORTEM,
                op_id="op-pm",
                seq=2,
                payload={
                    "root_cause": "syntax_error in foo.py",
                    "failed_phase": "VALIDATE",
                    "next_safe_action": "review_code",
                },
            )
        )

        trace = fake.traces["op-pm"]
        assert len(trace.spans) == 1
        assert trace.spans[0].name == "postmortem"
        assert trace.spans[0].level == "ERROR"
        assert trace.spans[0].metadata["root_cause"] == "syntax_error in foo.py"
        # Trace updated
        assert trace.updates[0]["outcome"] == "postmortem"
        assert trace.updates[0]["error"] == "syntax_error in foo.py"
        assert trace.updates[0]["failed_phase"] == "VALIDATE"
        # Flushed
        assert fake.flush_count >= 1

    @pytest.mark.asyncio
    async def test_postmortem_cleans_up_op_state(self):
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake)

        await transport.send(
            _make_msg(MessageType.INTENT, op_id="op-pm-clean", payload={"goal": "x"})
        )
        await transport.send(
            _make_msg(
                MessageType.POSTMORTEM,
                op_id="op-pm-clean",
                seq=2,
                payload={"root_cause": "boom", "failed_phase": "GENERATE"},
            )
        )
        assert "op-pm-clean" not in transport._traces

    @pytest.mark.asyncio
    async def test_postmortem_without_trace_is_noop(self):
        """POSTMORTEM for an unknown op_id does not raise."""
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake)

        await transport.send(
            _make_msg(
                MessageType.POSTMORTEM,
                op_id="op-orphan-pm",
                payload={"root_cause": "x"},
            )
        )
        assert fake.flush_count == 0


# ---------------------------------------------------------------------------
# Tests: Fault isolation
# ---------------------------------------------------------------------------


class TestFaultIsolation:
    """Transport errors must never propagate to CommProtocol."""

    @pytest.mark.asyncio
    async def test_send_swallows_exceptions(self):
        """If the Langfuse client throws, send() does not raise."""
        broken_client = MagicMock()
        broken_client.trace.side_effect = RuntimeError("Langfuse API down")
        transport = LangfuseTransport(langfuse_client=broken_client)

        msg = _make_msg(MessageType.INTENT, payload={"goal": "explode"})
        # Must not raise
        await transport.send(msg)

    @pytest.mark.asyncio
    async def test_broken_span_does_not_block_subsequent_messages(self):
        """A failing span call does not prevent future messages."""
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake)

        # Create trace
        await transport.send(
            _make_msg(MessageType.INTENT, op_id="op-broken", payload={"goal": "x"})
        )

        # Make span() blow up
        trace = fake.traces["op-broken"]
        original_span = trace.span
        trace.span = MagicMock(side_effect=RuntimeError("span failed"))

        # PLAN should fail silently
        await transport.send(
            _make_msg(MessageType.PLAN, op_id="op-broken", seq=2, payload={"source": "y"})
        )

        # Restore and verify we can still send
        trace.span = original_span
        await transport.send(
            _make_msg(MessageType.PLAN, op_id="op-broken", seq=3, payload={"source": "z"})
        )
        assert len(trace.spans) == 1
        assert trace.spans[0].name == "plan-z"

    @pytest.mark.asyncio
    async def test_works_alongside_other_transports_in_comm_protocol(self):
        """LangfuseTransport can coexist with LogTransport in CommProtocol."""
        fake = FakeLangfuse()
        lf_transport = LangfuseTransport(langfuse_client=fake)
        log_transport = LogTransport()

        proto = CommProtocol(transports=[log_transport, lf_transport])

        await proto.emit_intent(
            op_id="op-dual-lf",
            goal="dual test",
            target_files=["a.py"],
            risk_tier="SAFE_AUTO",
            blast_radius=1,
        )

        # Both transports received the message
        assert len(log_transport.messages) == 1
        assert "op-dual-lf" in fake.traces


# ---------------------------------------------------------------------------
# Tests: Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    """shutdown() must flush pending traces."""

    @pytest.mark.asyncio
    async def test_shutdown_flushes(self):
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake)

        await transport.shutdown()
        assert fake.flush_count == 1

    @pytest.mark.asyncio
    async def test_shutdown_noop_when_inactive(self):
        """shutdown() on an inactive transport does not raise."""
        transport = LangfuseTransport(langfuse_client=None)
        await transport.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_swallows_flush_error(self):
        """If flush() throws during shutdown, the exception is swallowed."""
        broken_client = MagicMock()
        broken_client.flush.side_effect = RuntimeError("flush exploded")
        transport = LangfuseTransport(langfuse_client=broken_client)

        # Must not raise
        await transport.shutdown()


# ---------------------------------------------------------------------------
# Tests: Full lifecycle
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    """End-to-end test: INTENT -> PLAN -> HEARTBEAT -> DECISION."""

    @pytest.mark.asyncio
    async def test_complete_happy_path(self):
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake, heartbeat_sample_rate=1)
        op = "op-lifecycle"

        # 1. INTENT
        await transport.send(
            _make_msg(MessageType.INTENT, op_id=op, seq=1, payload={
                "goal": "Add tests",
                "target_files": ["test.py"],
                "risk_tier": "SAFE_AUTO",
                "blast_radius": 1,
            })
        )
        assert op in fake.traces

        # 2. PLAN
        await transport.send(
            _make_msg(MessageType.PLAN, op_id=op, seq=2, causal_parent_seq=1, payload={
                "source": "claude-api",
                "steps": ["generate", "apply"],
            })
        )

        # 3. HEARTBEAT (sample_rate=1, so every heartbeat recorded)
        await transport.send(
            _make_msg(MessageType.HEARTBEAT, op_id=op, seq=3, causal_parent_seq=2, payload={
                "phase": "GENERATE",
                "progress_pct": 50.0,
            })
        )

        # 4. DECISION
        await transport.send(
            _make_msg(MessageType.DECISION, op_id=op, seq=4, causal_parent_seq=3, payload={
                "outcome": "applied",
                "reason_code": "safe_auto",
                "provider_used": "claude-api",
            })
        )

        trace = fake.traces[op]
        # plan + heartbeat + decision = 3 spans
        assert len(trace.spans) == 3
        assert trace.spans[0].name == "plan-claude-api"
        assert trace.spans[1].name == "heartbeat"
        assert trace.spans[2].name == "decision-applied"

        # Trace updated with final outcome
        assert trace.updates[-1]["outcome"] == "applied"

        # Op cleaned up from transport internal state
        assert op not in transport._traces
        assert op not in transport._heartbeat_counters

        # Flushed
        assert fake.flush_count >= 1

    @pytest.mark.asyncio
    async def test_postmortem_lifecycle(self):
        """INTENT -> POSTMORTEM path (operation failed early)."""
        fake = FakeLangfuse()
        transport = LangfuseTransport(langfuse_client=fake)
        op = "op-fail-lifecycle"

        await transport.send(
            _make_msg(MessageType.INTENT, op_id=op, seq=1, payload={"goal": "break"})
        )
        await transport.send(
            _make_msg(MessageType.POSTMORTEM, op_id=op, seq=2, payload={
                "root_cause": "file not found",
                "failed_phase": "CONTEXT_EXPANSION",
            })
        )

        trace = fake.traces[op]
        assert len(trace.spans) == 1
        assert trace.spans[0].name == "postmortem"
        assert trace.spans[0].level == "ERROR"
        assert op not in transport._traces
        assert fake.flush_count >= 1
