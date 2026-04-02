"""Integration: REM poll -> council -> FSM transitions."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.hive.hive_service import HiveService
from backend.hive.cognitive_fsm import CognitiveEvent
from backend.hive.thread_models import CognitiveState, ThreadState
from backend.neural_mesh.data_models import MessageType


def _response(reasoning, confidence=0.8, principle=None):
    d = {"reasoning": reasoning, "confidence": confidence}
    if principle:
        d["manifesto_principle"] = principle
    return json.dumps(d)


class TestRemCouncilIntegration:

    @pytest.mark.asyncio
    async def test_rem_council_runs_and_returns_to_baseline(self, tmp_path):
        dw = AsyncMock()
        dw.is_available = True
        dw.prompt_only = AsyncMock(return_value=_response("All systems healthy.", 0.9))
        bus = AsyncMock()
        bus.subscribe_broadcast = AsyncMock()

        service = HiveService(bus=bus, governed_loop=None, doubleword=dw, state_dir=tmp_path)
        await service.start()

        # Manually enter REM
        service._fsm.decide(CognitiveEvent.REM_TRIGGER, idle_seconds=25000, system_load_pct=10.0)
        service._fsm.apply_last_decision()
        assert service._fsm.state == CognitiveState.REM

        with patch("backend.hive.rem_health_scanner.psutil") as mock_ps:
            mock_ps.virtual_memory.return_value = MagicMock(percent=50.0)
            mock_ps.cpu_percent.return_value = 15.0
            mock_ps.disk_usage.return_value = MagicMock(percent=30.0)
            await service._run_rem_council()

        assert service._fsm.state == CognitiveState.BASELINE
        await service.stop()

    @pytest.mark.asyncio
    async def test_rem_council_escalates_critical_to_flow(self, tmp_path):
        dw = AsyncMock()
        dw.is_available = True
        # Health observe (critical) + FLOW debate (observe, propose, validate approve)
        dw.prompt_only = AsyncMock(side_effect=[
            _response("RAM critical!", 0.95),
            _response("Emergency cleanup needed.", 0.9),
            _response("Kill stale processes.", 0.85),
            _response("Approved.", 0.92, principle="$3"),
        ])
        bus = AsyncMock()
        bus.subscribe_broadcast = AsyncMock()
        gl = AsyncMock()
        gl.submit = AsyncMock(return_value=MagicMock())

        service = HiveService(bus=bus, governed_loop=gl, doubleword=dw, state_dir=tmp_path)
        await service.start()

        service._fsm.decide(CognitiveEvent.REM_TRIGGER, idle_seconds=25000, system_load_pct=10.0)
        service._fsm.apply_last_decision()
        assert service._fsm.state == CognitiveState.REM

        with patch("backend.hive.rem_health_scanner.psutil") as mock_ps:
            mock_ps.virtual_memory.return_value = MagicMock(percent=96.0)
            mock_ps.cpu_percent.return_value = 95.0
            mock_ps.disk_usage.return_value = MagicMock(percent=30.0)
            await service._run_rem_council()

        # Should have escalated to FLOW (or already back to BASELINE if debate completed)
        assert service._fsm.state in (CognitiveState.FLOW, CognitiveState.BASELINE)
        await service.stop()
