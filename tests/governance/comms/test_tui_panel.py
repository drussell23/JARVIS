"""tests/governance/comms/test_tui_panel.py"""
import time
import pytest
from unittest.mock import MagicMock


def _make_comm_message(msg_type, op_id="op-001", payload=None):
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    return CommMessage(
        msg_type=MessageType[msg_type] if isinstance(msg_type, str) else msg_type,
        op_id=op_id,
        seq=1,
        causal_parent_seq=None,
        payload=payload or {},
        timestamp=time.time(),
    )


class TestTUIPanelState:
    @pytest.mark.asyncio
    async def test_intent_creates_active_op(self):
        from backend.core.ouroboros.governance.comms.tui_panel import TUISelfProgramPanel

        panel = TUISelfProgramPanel()
        msg = _make_comm_message("INTENT", op_id="op-001", payload={
            "goal": "fix test",
            "target_files": ["tests/test_a.py"],
            "risk_tier": "SAFE_AUTO",
        })
        await panel.send(msg)

        state = panel.get_state()
        assert len(state.active_ops) == 1
        assert state.active_ops[0].op_id == "op-001"

    @pytest.mark.asyncio
    async def test_decision_completes_op(self):
        from backend.core.ouroboros.governance.comms.tui_panel import TUISelfProgramPanel

        panel = TUISelfProgramPanel()
        intent = _make_comm_message("INTENT", op_id="op-001", payload={
            "goal": "fix test", "target_files": ["a.py"],
        })
        decision = _make_comm_message("DECISION", op_id="op-001", payload={
            "outcome": "applied", "reason_code": "tests_pass",
        })
        await panel.send(intent)
        await panel.send(decision)

        state = panel.get_state()
        assert len(state.active_ops) == 0
        assert len(state.recent_completions) == 1
        assert state.recent_completions[0].op_id == "op-001"
        assert state.recent_completions[0].outcome == "applied"

    @pytest.mark.asyncio
    async def test_postmortem_completes_op(self):
        from backend.core.ouroboros.governance.comms.tui_panel import TUISelfProgramPanel

        panel = TUISelfProgramPanel()
        intent = _make_comm_message("INTENT", op_id="op-002", payload={
            "goal": "fix bug", "target_files": ["b.py"],
        })
        postmortem = _make_comm_message("POSTMORTEM", op_id="op-002", payload={
            "root_cause": "AST parse failed",
        })
        await panel.send(intent)
        await panel.send(postmortem)

        state = panel.get_state()
        assert len(state.active_ops) == 0
        assert len(state.recent_completions) == 1
        assert state.recent_completions[0].outcome == "postmortem"

    @pytest.mark.asyncio
    async def test_heartbeat_updates_phase(self):
        from backend.core.ouroboros.governance.comms.tui_panel import TUISelfProgramPanel

        panel = TUISelfProgramPanel()
        intent = _make_comm_message("INTENT", op_id="op-001", payload={
            "goal": "fix", "target_files": ["a.py"],
        })
        heartbeat = _make_comm_message("HEARTBEAT", op_id="op-001", payload={
            "phase": "generating",
            "progress_pct": 50,
        })
        await panel.send(intent)
        await panel.send(heartbeat)

        state = panel.get_state()
        assert state.active_ops[0].phase == "generating"

    @pytest.mark.asyncio
    async def test_recent_completions_capped_at_10(self):
        from backend.core.ouroboros.governance.comms.tui_panel import TUISelfProgramPanel

        panel = TUISelfProgramPanel()
        for i in range(15):
            intent = _make_comm_message("INTENT", op_id=f"op-{i:03d}", payload={
                "goal": f"fix {i}", "target_files": [f"f{i}.py"],
            })
            decision = _make_comm_message("DECISION", op_id=f"op-{i:03d}", payload={
                "outcome": "applied",
            })
            await panel.send(intent)
            await panel.send(decision)

        state = panel.get_state()
        assert len(state.recent_completions) == 10

    @pytest.mark.asyncio
    async def test_ops_today_counter(self):
        from backend.core.ouroboros.governance.comms.tui_panel import TUISelfProgramPanel

        panel = TUISelfProgramPanel()
        for i in range(3):
            intent = _make_comm_message("INTENT", op_id=f"op-{i}", payload={
                "goal": "fix", "target_files": ["a.py"],
            })
            decision = _make_comm_message("DECISION", op_id=f"op-{i}", payload={
                "outcome": "applied",
            })
            await panel.send(intent)
            await panel.send(decision)

        state = panel.get_state()
        assert state.ops_today == 3


class TestTUIPanelTransport:
    @pytest.mark.asyncio
    async def test_unknown_op_heartbeat_ignored(self):
        from backend.core.ouroboros.governance.comms.tui_panel import TUISelfProgramPanel

        panel = TUISelfProgramPanel()
        heartbeat = _make_comm_message("HEARTBEAT", op_id="op-unknown", payload={
            "phase": "generating",
        })
        await panel.send(heartbeat)  # should not crash

        state = panel.get_state()
        assert len(state.active_ops) == 0
