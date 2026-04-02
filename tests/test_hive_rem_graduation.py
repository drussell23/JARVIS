"""
Tests for REM Graduation Auditor -- Ouroboros ledger scanning.

Covers:
- Empty ledger -> no candidates, no stale, no threads
- 3+ completed ops -> graduation candidate thread created
- Stale ops (wall_time > 30 days ago) -> detected in _scan_ledger
- 6+ completed ops -> escalation (should_escalate=True)
- Respects budget
- No candidates + no stale -> empty result
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.hive.rem_graduation_auditor import GraduationAuditor
from backend.hive.hud_relay_agent import HudRelayAgent
from backend.hive.thread_manager import ThreadManager
from backend.hive.thread_models import CognitiveState, PersonaIntent


# ============================================================================
# Helpers
# ============================================================================


def _make_ledger_entry(
    op_id: str, state: str = "completed", wall_time: float | None = None
) -> str:
    """Build a single JSONL ledger line."""
    return json.dumps(
        {
            "op_id": op_id,
            "state": state,
            "wall_time": wall_time or time.time(),
            "data": {},
        }
    )


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def ledger_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ledger"
    d.mkdir()
    return d


@pytest.fixture
def mock_engine() -> MagicMock:
    engine = MagicMock()
    engine.generate_reasoning = AsyncMock(
        return_value=MagicMock(
            type="persona_reasoning",
            persona="jarvis",
            role="body",
            intent=PersonaIntent.OBSERVE,
            reasoning="3 tools ready for graduation.",
            confidence=0.85,
            token_cost=200,
            message_id="msg_grad",
            manifesto_principle="$6 Neuroplasticity",
            validate_verdict=None,
            to_dict=lambda: {"type": "persona_reasoning"},
        )
    )
    return engine


@pytest.fixture
def thread_mgr(tmp_path: Path) -> ThreadManager:
    return ThreadManager(storage_dir=tmp_path / "threads")


@pytest.fixture
def relay() -> HudRelayAgent:
    r = HudRelayAgent()
    r._ipc_send = AsyncMock()
    return r


@pytest.fixture
def auditor(
    mock_engine: MagicMock,
    thread_mgr: ThreadManager,
    relay: HudRelayAgent,
    ledger_dir: Path,
) -> GraduationAuditor:
    return GraduationAuditor(
        persona_engine=mock_engine,
        thread_manager=thread_mgr,
        relay=relay,
        ledger_dir=ledger_dir,
    )


# ============================================================================
# _scan_ledger tests
# ============================================================================


class TestLedgerScanning:
    """Direct tests for _scan_ledger logic."""

    def test_empty_ledger_no_candidates(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """Empty ledger directory returns zero counts and no stale ops."""
        status_counts, stale = auditor._scan_ledger()
        assert len(status_counts) == 0
        assert len(stale) == 0

    def test_nonexistent_ledger_dir(self, mock_engine, thread_mgr, relay, tmp_path):
        """If ledger_dir does not exist, returns empty without error."""
        a = GraduationAuditor(
            persona_engine=mock_engine,
            thread_manager=thread_mgr,
            relay=relay,
            ledger_dir=tmp_path / "does_not_exist",
        )
        status_counts, stale = a._scan_ledger()
        assert len(status_counts) == 0
        assert len(stale) == 0

    def test_detects_graduation_candidate(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """3+ completed op files -> status_counts['completed'] >= 3."""
        for i in range(3):
            f = ledger_dir / f"op-test-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-test-{i}", "completed"))
        status_counts, _ = auditor._scan_ledger()
        assert status_counts.get("completed", 0) >= 3

    def test_detects_stale_ops(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """Ops with wall_time > 30 days ago appear in stale list."""
        old_time = time.time() - (31 * 86400)
        f = ledger_dir / "op-old-0-jarvis.jsonl"
        f.write_text(_make_ledger_entry("op-old-0", "completed", wall_time=old_time))
        _, stale = auditor._scan_ledger()
        assert len(stale) >= 1
        assert "op-old-0-jarvis" in stale

    def test_recent_ops_not_stale(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """Ops with recent wall_time are NOT considered stale."""
        f = ledger_dir / "op-fresh-0-jarvis.jsonl"
        f.write_text(_make_ledger_entry("op-fresh-0", "completed"))
        _, stale = auditor._scan_ledger()
        assert len(stale) == 0

    def test_multi_line_ledger_uses_latest(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """When a file has multiple lines, the latest wall_time determines state."""
        lines = [
            _make_ledger_entry("op-multi", "started", wall_time=1000.0),
            _make_ledger_entry("op-multi", "completed", wall_time=2000.0),
        ]
        f = ledger_dir / "op-multi-0-jarvis.jsonl"
        f.write_text("\n".join(lines))

        # wall_time 2000.0 is very old (1970) -> stale
        status_counts, stale = auditor._scan_ledger()
        assert status_counts.get("completed", 0) == 1
        assert "op-multi-0-jarvis" in stale

    def test_mixed_states(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """Multiple files with different final states produce correct counts."""
        for i in range(3):
            f = ledger_dir / f"op-comp-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-comp-{i}", "completed"))
        for i in range(2):
            f = ledger_dir / f"op-fail-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-fail-{i}", "failed"))

        status_counts, _ = auditor._scan_ledger()
        assert status_counts["completed"] == 3
        assert status_counts["failed"] == 2

    def test_malformed_json_skipped(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """Files with invalid JSON are silently skipped."""
        f = ledger_dir / "op-bad-0-jarvis.jsonl"
        f.write_text("not valid json {{{")
        # Also add a valid file so we can confirm it still works
        f2 = ledger_dir / "op-good-0-jarvis.jsonl"
        f2.write_text(_make_ledger_entry("op-good-0", "completed"))

        status_counts, _ = auditor._scan_ledger()
        assert status_counts.get("completed", 0) == 1

    def test_ignores_non_op_files(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """Files not matching op-*.jsonl glob are ignored."""
        f = ledger_dir / "summary.json"
        f.write_text(json.dumps({"total": 10}))
        f2 = ledger_dir / "other-thing.jsonl"
        f2.write_text(_make_ledger_entry("x", "completed"))

        status_counts, _ = auditor._scan_ledger()
        assert len(status_counts) == 0


# ============================================================================
# run() tests
# ============================================================================


class TestGraduationRun:
    """Async tests for the full run() method."""

    @pytest.mark.asyncio
    async def test_no_candidates_no_threads(self, auditor: GraduationAuditor):
        """Empty ledger produces no threads, zero calls, no escalation."""
        thread_ids, calls, escalate, esc_id = await auditor.run(budget=15)
        assert thread_ids == []
        assert calls == 0
        assert escalate is False
        assert esc_id is None

    @pytest.mark.asyncio
    async def test_below_threshold_no_thread(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """Fewer than 3 completed ops does NOT create a graduation thread."""
        for i in range(2):
            f = ledger_dir / f"op-low-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-low-{i}", "completed"))
        thread_ids, calls, escalate, esc_id = await auditor.run(budget=15)
        assert thread_ids == []
        assert calls == 0
        assert escalate is False

    @pytest.mark.asyncio
    async def test_candidates_create_thread(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """3+ completed ops creates a graduation thread."""
        for i in range(4):
            f = ledger_dir / f"op-grad-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-grad-{i}", "completed"))
        thread_ids, calls, escalate, esc_id = await auditor.run(budget=15)
        assert len(thread_ids) >= 1
        assert calls >= 1

    @pytest.mark.asyncio
    async def test_strong_candidates_escalate(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """5+ completed ops -> strong signal -> should_escalate=True."""
        for i in range(6):
            f = ledger_dir / f"op-strong-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-strong-{i}", "completed"))
        thread_ids, calls, escalate, esc_id = await auditor.run(budget=15)
        assert escalate is True
        assert esc_id is not None
        assert esc_id in thread_ids

    @pytest.mark.asyncio
    async def test_exactly_threshold_no_escalation(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """Exactly 3 completed (below strong threshold) -> no escalation."""
        for i in range(3):
            f = ledger_dir / f"op-exact-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-exact-{i}", "completed"))
        thread_ids, calls, escalate, esc_id = await auditor.run(budget=15)
        assert len(thread_ids) >= 1
        assert escalate is False
        assert esc_id is None

    @pytest.mark.asyncio
    async def test_stale_ops_create_thread(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """Stale ops (>30 days) create a separate stale-tools thread."""
        old_time = time.time() - (31 * 86400)
        for i in range(2):
            f = ledger_dir / f"op-stale-{i}-jarvis.jsonl"
            f.write_text(
                _make_ledger_entry(f"op-stale-{i}", "completed", wall_time=old_time)
            )
        thread_ids, calls, _, _ = await auditor.run(budget=15)
        # 2 completed (below graduation threshold) but stale -> stale thread only
        assert len(thread_ids) >= 1
        assert calls >= 1

    @pytest.mark.asyncio
    async def test_graduation_and_stale_separate_threads(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """Both graduation candidates and stale ops each get their own thread."""
        # 3 recent completed -> graduation thread
        for i in range(3):
            f = ledger_dir / f"op-recent-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-recent-{i}", "completed"))
        # 1 stale completed -> stale thread
        old_time = time.time() - (31 * 86400)
        f = ledger_dir / "op-old-0-jarvis.jsonl"
        f.write_text(_make_ledger_entry("op-old-0", "completed", wall_time=old_time))

        thread_ids, calls, _, _ = await auditor.run(budget=15)
        # 4 completed total >= 3 -> graduation thread
        # 1 stale -> stale thread
        assert len(thread_ids) == 2
        assert calls == 2

    @pytest.mark.asyncio
    async def test_respects_budget(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """Calls used never exceed the budget."""
        for i in range(3):
            f = ledger_dir / f"op-budget-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-budget-{i}", "completed"))
        _, calls, _, _ = await auditor.run(budget=15)
        assert calls <= 15

    @pytest.mark.asyncio
    async def test_zero_budget_skips_all(
        self, auditor: GraduationAuditor, ledger_dir: Path
    ):
        """Budget of 0 means no threads can be created."""
        for i in range(5):
            f = ledger_dir / f"op-zero-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-zero-{i}", "completed"))
        thread_ids, calls, escalate, esc_id = await auditor.run(budget=0)
        assert thread_ids == []
        assert calls == 0

    @pytest.mark.asyncio
    async def test_persona_engine_called_with_observe(
        self,
        auditor: GraduationAuditor,
        mock_engine: MagicMock,
        ledger_dir: Path,
    ):
        """PersonaEngine.generate_reasoning is called with OBSERVE intent."""
        for i in range(3):
            f = ledger_dir / f"op-pe-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-pe-{i}", "completed"))
        await auditor.run(budget=15)
        mock_engine.generate_reasoning.assert_called()
        call_args = mock_engine.generate_reasoning.call_args
        assert call_args[0][0] == "jarvis"
        assert call_args[0][1] == PersonaIntent.OBSERVE

    @pytest.mark.asyncio
    async def test_thread_transitions_to_debating(
        self,
        auditor: GraduationAuditor,
        thread_mgr: ThreadManager,
        ledger_dir: Path,
    ):
        """Created thread is transitioned to DEBATING state."""
        for i in range(3):
            f = ledger_dir / f"op-trans-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-trans-{i}", "completed"))
        thread_ids, _, _, _ = await auditor.run(budget=15)
        assert len(thread_ids) >= 1
        thread = thread_mgr.get_thread(thread_ids[0])
        assert thread is not None
        from backend.hive.thread_models import ThreadState

        assert thread.state == ThreadState.DEBATING
