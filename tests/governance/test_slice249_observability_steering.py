"""Slice 249 — Asynchronous Observability Stream & Live Steering Protocol.

Verify-first scope (the brief over-claims Phase 1):
  * Phase 1 — the async non-blocking telemetry mesh ALREADY EXISTS
    (ide_observability_stream.StreamEventBroker.publish, SSE /observability/stream,
    phase transitions via phase_orchestra.emit_cue). print() is NOT in the hot
    path. Genuine gap: no DRIFT_DETECTED / TOOL_EXPLORATION_START / GUIDANCE
    event types yet → add them + publish wrappers (reuse the broker).
  * Phase 2 — live mid-flight steering is GENUINELY NEW. New steering.py
    GuidanceStore (op-id-keyed, mirrors Slice 246 preemption.py) + the
    tool_executor round-boundary absorbs guidance into the live prompt WITHOUT
    suspending the lane.
  * Phase 3 — the hash-chain concern is a NON-ISSUE by design: the Slice 248
    guillotine compares generate_file_hashes (disk files), orthogonal to the
    context_hash chain. with_steering_guidance updates context_hash via the
    standard with_* helper and never touches generate_file_hashes — proven here.
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance import steering as st
from backend.core.ouroboros.governance import ide_observability_stream as ios
from backend.core.ouroboros.governance import state_drift as sd
from backend.core.ouroboros.governance.op_context import OperationContext


def _ctx():
    return OperationContext.create(target_files=("backend/core/x.py",), description="op")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("JARVIS_LIVE_STEERING_ENABLED", raising=False)
    st.reset_guidance()
    yield
    st.reset_guidance()


class TestGuidanceStore:
    def test_inject_has_consume(self):
        assert st.has_guidance("op-1") is False
        st.inject_guidance("op-1", "prefer the async approach")
        assert st.has_guidance("op-1") is True
        out = st.consume_guidance("op-1")
        assert out == "prefer the async approach"
        # consumed once — drained
        assert st.has_guidance("op-1") is False
        assert st.consume_guidance("op-1") is None

    def test_multiple_injects_drain_together(self):
        st.inject_guidance("op-2", "first")
        st.inject_guidance("op-2", "second")
        out = st.consume_guidance("op-2")
        assert "first" in out and "second" in out

    def test_per_op_isolation(self):
        st.inject_guidance("op-a", "for-a")
        assert st.consume_guidance("op-b") is None
        assert st.consume_guidance("op-a") == "for-a"

    def test_gate_default_true_and_kill_switch(self, monkeypatch):
        assert st.live_steering_enabled() is True
        monkeypatch.setenv("JARVIS_LIVE_STEERING_ENABLED", "0")
        assert st.live_steering_enabled() is False

    def test_never_raises_on_bad_input(self):
        st.inject_guidance("", "x")  # empty op_id ignored
        assert st.consume_guidance("") is None

    def test_format_guidance_block_ascii_and_marked(self):
        block = st.format_guidance_block("use dependency injection")
        assert "use dependency injection" in block
        assert "GUIDANCE" in block
        assert block.isascii()


class TestTelemetryEvents:
    def test_new_event_types_registered(self):
        for ev in (ios.EVENT_TYPE_DRIFT_DETECTED,
                   ios.EVENT_TYPE_TOOL_EXPLORATION_START,
                   ios.EVENT_TYPE_GUIDANCE_ABSORBED):
            assert ev in ios._VALID_EVENT_TYPES

    def test_publish_wrappers_nonblocking_return_ids(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        ids = [
            ios.publish_drift_detected(op_id="op-x", drifted_files=["a.py"]),
            ios.publish_tool_exploration_start(op_id="op-x", round_index=1),
            ios.publish_guidance_absorbed(op_id="op-x", chars=42),
        ]
        # non-blocking publish accepted all 3 → distinct event ids
        assert all(i is not None for i in ids), ids
        assert len(set(ids)) == 3

    def test_publish_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "0")
        assert ios.publish_drift_detected(op_id="op", drifted_files=["a.py"]) is None

    def test_publish_never_raises(self):
        # bad payloads must never propagate
        ios.publish_drift_detected(op_id="op", drifted_files=None)  # type: ignore[arg-type]
        ios.publish_guidance_absorbed(op_id="op", chars=-1)


class TestContextSteeringHashChain:
    def test_with_steering_guidance_updates_context_not_file_hashes(self):
        import dataclasses
        base = dataclasses.replace(
            _ctx(), generate_file_hashes=(("backend/core/x.py", "abc123"),),
        )
        steered = base.with_steering_guidance("focus on the retry path")
        # guidance landed in the prompt-injection channel
        assert "focus on the retry path" in steered.strategic_memory_prompt
        # context_hash chain advanced cleanly (Phase 3 — clean mutation)
        assert steered.context_hash != base.context_hash
        assert steered.previous_hash == base.context_hash
        # file-drift baseline is UNTOUCHED → the Slice 248 guillotine is orthogonal
        assert steered.generate_file_hashes == base.generate_file_hashes

    def test_steering_does_not_trip_drift_guillotine(self, tmp_path, monkeypatch):
        import dataclasses
        import hashlib
        f = tmp_path / "x.py"
        f.write_text("stable\n")
        h = hashlib.sha256(b"stable\n").hexdigest()
        base = dataclasses.replace(_ctx(), generate_file_hashes=(("x.py", h),))
        steered = base.with_steering_guidance("human steering note")
        # the file did NOT change — only the context did. should_block_apply must
        # NOT fire on a steering update (proves Phase 3's concern is a non-issue).
        block, drifted = sd.should_block_apply(steered.generate_file_hashes, tmp_path)
        assert block is False and drifted == []


class TestToolExecutorWiring:
    def test_round_loop_absorbs_guidance(self):
        from backend.core.ouroboros.governance import tool_executor as te
        src = inspect.getsource(te)
        assert "consume_guidance" in src, "round loop must poll the guidance store"
        assert "live_steering_enabled" in src, "absorption must be gated"
        # absorbed at the boundary, folded into the live prompt
        assert "current_prompt" in src


class TestPhase4Integration:
    def test_emit_three_then_steer_then_absorb(self, monkeypatch):
        """Emit 3 async telemetry events (non-blocking) → inject guidance
        mid-op → consume at the boundary → fold into the live prompt, no
        suspension."""
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        op = "op-phase4"
        # 1) three non-blocking telemetry events
        e1 = ios.publish_tool_exploration_start(op_id=op, round_index=0)
        e2 = ios.publish_drift_detected(op_id=op, drifted_files=["app.py"])
        e3 = ios.publish_tool_exploration_start(op_id=op, round_index=1)
        assert all(e is not None for e in (e1, e2, e3))

        # 2) human injects guidance into the running op
        st.inject_guidance(op, "switch to the event-driven design")
        assert st.has_guidance(op) is True

        # 3) the running loop absorbs it at the boundary (simulated): consume +
        #    fold into the live prompt WITHOUT suspending the lane
        live_prompt = "ROUND 2 PROMPT BODY"
        guidance = st.consume_guidance(op)
        assert guidance is not None
        if st.live_steering_enabled():
            live_prompt = live_prompt + "\n\n" + st.format_guidance_block(guidance)
        assert "switch to the event-driven design" in live_prompt
        # the lane was never suspended — guidance drained, op continues
        assert st.has_guidance(op) is False
