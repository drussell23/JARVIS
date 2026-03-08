"""tests/governance/comms/test_e2e_comms.py

End-to-end: CommProtocol emits messages through all 3 transports
(VoiceNarrator, OpsLogger, TUISelfProgramPanel) simultaneously.
"""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock

from backend.core.ouroboros.governance.comm_protocol import (
    CommMessage,
    CommProtocol,
    MessageType,
)
from backend.core.ouroboros.governance.comms import (
    VoiceNarrator,
    OpsLogger,
    TUISelfProgramPanel,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_comm_message(msg_type, op_id="op-001", seq=1, payload=None):
    return CommMessage(
        msg_type=MessageType[msg_type] if isinstance(msg_type, str) else msg_type,
        op_id=op_id,
        seq=seq,
        causal_parent_seq=None,
        payload=payload or {},
        timestamp=time.time(),
    )


def _build_transports(tmp_path, mock_say=None):
    """Construct all 3 transports with sensible test defaults."""
    if mock_say is None:
        mock_say = AsyncMock(return_value=True)
    narrator = VoiceNarrator(say_fn=mock_say, debounce_s=0.0)
    ops_logger = OpsLogger(log_dir=tmp_path)
    tui_panel = TUISelfProgramPanel()
    return narrator, ops_logger, tui_panel, mock_say


# ---------------------------------------------------------------------------
# Direct transport delivery tests
# ---------------------------------------------------------------------------


class TestDirectDelivery:
    """Deliver messages directly to each transport's send() method."""

    @pytest.mark.asyncio
    async def test_all_transports_receive_intent(self, tmp_path):
        narrator, ops_logger, tui_panel, mock_say = _build_transports(tmp_path)

        msg = _make_comm_message("INTENT", op_id="op-e2e", payload={
            "goal": "fix edge case",
            "target_files": ["tests/test_utils.py"],
            "risk_tier": "SAFE_AUTO",
        })

        # Deliver to all 3 transports concurrently
        await asyncio.gather(
            narrator.send(msg),
            ops_logger.send(msg),
            tui_panel.send(msg),
        )

        # Voice narrator spoke (INTENT is a narrated type)
        mock_say.assert_called_once()

        # Ops logger wrote a daily log file containing the op_id
        log_files = list(tmp_path.glob("*.log"))
        assert len(log_files) == 1
        content = log_files[0].read_text()
        assert "op-e2e" in content
        assert "INTENT" in content

        # TUI panel tracks the active operation
        state = tui_panel.get_state()
        assert len(state.active_ops) == 1
        assert state.active_ops[0].op_id == "op-e2e"
        assert state.active_ops[0].phase == "intent"

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, tmp_path):
        """INTENT -> HEARTBEAT -> DECISION flows through all transports."""
        narrator, ops_logger, tui_panel, mock_say = _build_transports(tmp_path)
        transports = [narrator, ops_logger, tui_panel]

        # 1. INTENT
        intent = _make_comm_message("INTENT", op_id="op-lc", payload={
            "goal": "fix bug",
            "target_files": ["a.py"],
            "risk_tier": "SAFE_AUTO",
        })
        for t in transports:
            await t.send(intent)

        assert len(tui_panel.get_state().active_ops) == 1
        assert tui_panel.get_state().active_ops[0].phase == "intent"

        # 2. HEARTBEAT — narrator skips, ops_logger writes, TUI updates phase
        heartbeat = _make_comm_message("HEARTBEAT", op_id="op-lc", seq=2, payload={
            "phase": "generating",
            "progress_pct": 50,
        })
        for t in transports:
            await t.send(heartbeat)

        assert tui_panel.get_state().active_ops[0].phase == "generating"

        # 3. DECISION (applied) — terminal: TUI moves op to completions
        decision = _make_comm_message("DECISION", op_id="op-lc", seq=3, payload={
            "outcome": "applied",
            "reason_code": "tests_pass",
        })
        for t in transports:
            await t.send(decision)

        state = tui_panel.get_state()
        assert len(state.active_ops) == 0
        assert len(state.recent_completions) == 1
        assert state.recent_completions[0].op_id == "op-lc"
        assert state.recent_completions[0].outcome == "applied"
        assert state.ops_today == 1

        # Narrator spoke for INTENT and DECISION (not HEARTBEAT)
        assert mock_say.call_count == 2

        # Ops logger captured all 3 message types
        content = list(tmp_path.glob("*.log"))[0].read_text()
        assert "INTENT" in content
        assert "HEARTBEAT" in content
        assert "DECISION" in content

    @pytest.mark.asyncio
    async def test_postmortem_lifecycle(self, tmp_path):
        """INTENT -> POSTMORTEM flows correctly through all transports."""
        narrator, ops_logger, tui_panel, mock_say = _build_transports(tmp_path)
        transports = [narrator, ops_logger, tui_panel]

        intent = _make_comm_message("INTENT", op_id="op-pm", payload={
            "goal": "refactor module",
            "target_files": ["core.py"],
            "risk_tier": "REVIEW_REQUIRED",
        })
        for t in transports:
            await t.send(intent)

        postmortem = _make_comm_message("POSTMORTEM", op_id="op-pm", seq=2, payload={
            "root_cause": "test regression",
            "failed_phase": "validation",
            "next_safe_action": "retry with smaller scope",
        })
        for t in transports:
            await t.send(postmortem)

        # TUI: op completed via postmortem
        state = tui_panel.get_state()
        assert len(state.active_ops) == 0
        assert len(state.recent_completions) == 1
        assert state.recent_completions[0].outcome == "postmortem"
        assert state.ops_today == 1

        # Narrator spoke for both INTENT and POSTMORTEM
        assert mock_say.call_count == 2

        # Ops logger has both entries
        content = list(tmp_path.glob("*.log"))[0].read_text()
        assert "INTENT" in content
        assert "POSTMORTEM" in content
        assert "test regression" in content


# ---------------------------------------------------------------------------
# CommProtocol-driven tests (messages flow through _emit -> all transports)
# ---------------------------------------------------------------------------


class TestCommProtocolDriven:
    """Use CommProtocol to emit messages; verify all transports receive them."""

    @pytest.mark.asyncio
    async def test_protocol_emits_to_all_transports(self, tmp_path):
        narrator, ops_logger, tui_panel, mock_say = _build_transports(tmp_path)
        protocol = CommProtocol(transports=[narrator, ops_logger, tui_panel])

        await protocol.emit_intent(
            op_id="op-proto",
            goal="optimize loop",
            target_files=["engine.py"],
            risk_tier="SAFE_AUTO",
            blast_radius=1,
        )

        # All transports received the INTENT
        mock_say.assert_called_once()

        log_content = list(tmp_path.glob("*.log"))[0].read_text()
        assert "op-proto" in log_content

        state = tui_panel.get_state()
        assert len(state.active_ops) == 1
        assert state.active_ops[0].op_id == "op-proto"

    @pytest.mark.asyncio
    async def test_protocol_full_5phase_lifecycle(self, tmp_path):
        """Full INTENT -> PLAN -> HEARTBEAT -> DECISION -> POSTMORTEM cycle."""
        narrator, ops_logger, tui_panel, mock_say = _build_transports(tmp_path)
        protocol = CommProtocol(transports=[narrator, ops_logger, tui_panel])

        op = "op-full"

        # Phase 1: INTENT
        await protocol.emit_intent(
            op_id=op,
            goal="add retry logic",
            target_files=["client.py"],
            risk_tier="SAFE_AUTO",
            blast_radius=1,
        )
        assert len(tui_panel.get_state().active_ops) == 1

        # Phase 2: PLAN — narrator skips, logger writes, TUI ignores (no handler)
        await protocol.emit_plan(
            op_id=op,
            steps=["parse AST", "insert try/except", "run tests"],
            rollback_strategy="git stash pop",
        )

        # Phase 3: HEARTBEAT — narrator skips, logger writes, TUI updates phase
        await protocol.emit_heartbeat(
            op_id=op,
            phase="generating",
            progress_pct=60,
        )
        assert tui_panel.get_state().active_ops[0].phase == "generating"

        # Phase 4: DECISION — terminal for TUI
        await protocol.emit_decision(
            op_id=op,
            outcome="applied",
            reason_code="tests_pass",
            diff_summary="+3 -1",
        )
        state = tui_panel.get_state()
        assert len(state.active_ops) == 0
        assert state.ops_today == 1

        # Phase 5: POSTMORTEM after decision (unusual but valid)
        await protocol.emit_postmortem(
            op_id=op,
            root_cause="n/a",
            failed_phase=None,
            next_safe_action=None,
        )

        # Narrator: spoke for INTENT, DECISION, POSTMORTEM (3 narrated types)
        assert mock_say.call_count == 3

        # Logger captured all 5 phases
        content = list(tmp_path.glob("*.log"))[0].read_text()
        for phase_name in ("INTENT", "PLAN", "HEARTBEAT", "DECISION", "POSTMORTEM"):
            assert phase_name in content, f"{phase_name} missing from ops log"

    @pytest.mark.asyncio
    async def test_protocol_fault_isolation(self, tmp_path):
        """A broken transport does not prevent delivery to healthy ones."""
        mock_say = AsyncMock(return_value=True)
        narrator = VoiceNarrator(say_fn=mock_say, debounce_s=0.0)
        tui_panel = TUISelfProgramPanel()

        # Create a transport that always raises
        class BrokenTransport:
            async def send(self, msg):
                raise RuntimeError("I am broken")

        protocol = CommProtocol(
            transports=[narrator, BrokenTransport(), tui_panel]
        )

        await protocol.emit_intent(
            op_id="op-fault",
            goal="test fault isolation",
            target_files=["x.py"],
            risk_tier="SAFE_AUTO",
            blast_radius=1,
        )

        # Healthy transports still received the message
        mock_say.assert_called_once()
        assert len(tui_panel.get_state().active_ops) == 1


# ---------------------------------------------------------------------------
# Concurrent / stress tests
# ---------------------------------------------------------------------------


class TestConcurrency:
    """Verify transports handle concurrent message delivery."""

    @pytest.mark.asyncio
    async def test_multiple_ops_concurrent(self, tmp_path):
        """Multiple independent operations delivered concurrently."""
        narrator, ops_logger, tui_panel, mock_say = _build_transports(tmp_path)
        transports = [narrator, ops_logger, tui_panel]

        ops = [f"op-c{i}" for i in range(5)]

        # Send all INTENTs concurrently
        tasks = []
        for op_id in ops:
            msg = _make_comm_message("INTENT", op_id=op_id, payload={
                "goal": f"fix {op_id}",
                "target_files": [f"{op_id}.py"],
                "risk_tier": "SAFE_AUTO",
            })
            for t in transports:
                tasks.append(t.send(msg))
        await asyncio.gather(*tasks)

        # TUI tracks all 5 active ops
        state = tui_panel.get_state()
        assert len(state.active_ops) == 5
        tracked_ids = {op.op_id for op in state.active_ops}
        assert tracked_ids == set(ops)

        # Narrator spoke for all 5 (debounce=0, all unique op_ids)
        assert mock_say.call_count == 5

        # Logger has all 5 op_ids
        content = list(tmp_path.glob("*.log"))[0].read_text()
        for op_id in ops:
            assert op_id in content

    @pytest.mark.asyncio
    async def test_rapid_heartbeats(self, tmp_path):
        """Rapid heartbeat stream updates TUI phase without errors."""
        _, ops_logger, tui_panel, _ = _build_transports(tmp_path)

        # Seed an active op
        intent = _make_comm_message("INTENT", op_id="op-hb", payload={
            "goal": "stress test",
            "target_files": ["stress.py"],
        })
        await tui_panel.send(intent)

        # Send 20 heartbeats with increasing progress
        for i in range(20):
            hb = _make_comm_message("HEARTBEAT", op_id="op-hb", seq=i + 2, payload={
                "phase": "generating",
                "progress_pct": (i + 1) * 5,
            })
            await tui_panel.send(hb)
            await ops_logger.send(hb)

        # TUI reflects final heartbeat state
        state = tui_panel.get_state()
        assert len(state.active_ops) == 1
        assert state.active_ops[0].phase == "generating"

    @pytest.mark.asyncio
    async def test_approval_flow(self, tmp_path):
        """Heartbeat with phase='approve' sets awaiting_approval flag."""
        _, _, tui_panel, _ = _build_transports(tmp_path)

        intent = _make_comm_message("INTENT", op_id="op-apr", payload={
            "goal": "dangerous refactor",
            "target_files": ["core.py"],
            "risk_tier": "REVIEW_REQUIRED",
        })
        await tui_panel.send(intent)

        approve_hb = _make_comm_message("HEARTBEAT", op_id="op-apr", seq=2, payload={
            "phase": "approve",
            "progress_pct": 100,
        })
        await tui_panel.send(approve_hb)

        state = tui_panel.get_state()
        assert len(state.pending_approvals) == 1
        assert state.pending_approvals[0].op_id == "op-apr"
        assert state.pending_approvals[0].awaiting_approval is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases across transport boundaries."""

    @pytest.mark.asyncio
    async def test_decision_for_unknown_op(self, tmp_path):
        """DECISION for an op never registered via INTENT does not crash."""
        narrator, ops_logger, tui_panel, mock_say = _build_transports(tmp_path)
        transports = [narrator, ops_logger, tui_panel]

        decision = _make_comm_message("DECISION", op_id="op-ghost", payload={
            "outcome": "applied",
            "reason_code": "magic",
        })
        for t in transports:
            await t.send(decision)

        # TUI still records it as a completion (with defaults)
        state = tui_panel.get_state()
        assert len(state.active_ops) == 0
        assert len(state.recent_completions) == 1
        assert state.recent_completions[0].op_id == "op-ghost"
        assert state.ops_today == 1

        # Logger wrote it
        content = list(tmp_path.glob("*.log"))[0].read_text()
        assert "op-ghost" in content

    @pytest.mark.asyncio
    async def test_narrator_idempotency_across_lifecycle(self, tmp_path):
        """Same op_id + msg_type is narrated only once even if sent twice."""
        narrator, _, _, mock_say = _build_transports(tmp_path)

        msg = _make_comm_message("INTENT", op_id="op-idem", payload={
            "goal": "fix once",
            "target_files": ["once.py"],
        })
        await narrator.send(msg)
        await narrator.send(msg)  # duplicate

        assert mock_say.call_count == 1

    @pytest.mark.asyncio
    async def test_empty_payload(self, tmp_path):
        """Transports handle messages with empty payloads gracefully."""
        narrator, ops_logger, tui_panel, mock_say = _build_transports(tmp_path)

        msg = _make_comm_message("INTENT", op_id="op-empty", payload={})
        await asyncio.gather(
            narrator.send(msg),
            ops_logger.send(msg),
            tui_panel.send(msg),
        )

        # Narrator still speaks (format_narration uses safe defaults)
        mock_say.assert_called_once()

        # TUI tracks with defaults
        state = tui_panel.get_state()
        assert len(state.active_ops) == 1
        assert state.active_ops[0].target_file == "unknown"

    @pytest.mark.asyncio
    async def test_say_fn_failure_does_not_block_other_transports(self, tmp_path):
        """When say_fn raises, the other transports are unaffected."""
        failing_say = AsyncMock(side_effect=RuntimeError("TTS engine crashed"))
        narrator = VoiceNarrator(say_fn=failing_say, debounce_s=0.0)
        ops_logger = OpsLogger(log_dir=tmp_path)
        tui_panel = TUISelfProgramPanel()

        protocol = CommProtocol(transports=[narrator, ops_logger, tui_panel])

        # VoiceNarrator internally catches say_fn errors, so protocol sees
        # no exception at all — all transports should still process
        await protocol.emit_intent(
            op_id="op-fail",
            goal="survive TTS crash",
            target_files=["survive.py"],
            risk_tier="SAFE_AUTO",
            blast_radius=1,
        )

        # Logger and TUI still work
        content = list(tmp_path.glob("*.log"))[0].read_text()
        assert "op-fail" in content

        state = tui_panel.get_state()
        assert len(state.active_ops) == 1
