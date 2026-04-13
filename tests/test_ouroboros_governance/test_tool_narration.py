"""Tests for ToolNarrationChannel — the sync-to-async narration bridge."""
from __future__ import annotations

import asyncio
import os
from typing import Any, List
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.tool_narration import (
    LIFECYCLE_STATUSES,
    PAYLOAD_KEYS,
    NarrationConfig,
    ToolNarrationChannel,
    _DuckMessage,
    build_args_summary,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _CollectingTransport:
    """Transport that appends every received message to an in-memory list."""

    def __init__(self) -> None:
        self.sent: List[Any] = []

    async def send(self, msg: Any) -> None:
        self.sent.append(msg)


class _FaultyTransport:
    """Transport whose send() always raises."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, msg: Any) -> None:
        self.attempts += 1
        raise RuntimeError("transport down")


class _DuckComm:
    """Minimal CommProtocol stand-in: _transports list, no _emit()."""

    def __init__(self, transports: List[Any]) -> None:
        self._transports = transports


class _EmittingComm:
    """CommProtocol stand-in that exposes _emit() so the channel uses it."""

    def __init__(self, transports: List[Any]) -> None:
        self._transports = transports
        self.emitted: List[Any] = []

    async def _emit(self, msg: Any) -> None:
        self.emitted.append(msg)
        for t in self._transports:
            await t.send(msg)


# ---------------------------------------------------------------------------
# NarrationConfig (env-driven)
# ---------------------------------------------------------------------------

class TestNarrationConfig:
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            for k in (
                "JARVIS_TOOL_NARRATION_ENABLED",
                "JARVIS_TOOL_NARRATION_MAX_PREVIEW",
                "JARVIS_TOOL_NARRATION_MAX_ARGS",
                "JARVIS_TOOL_NARRATION_WARN",
            ):
                os.environ.pop(k, None)
            cfg = NarrationConfig()
            assert cfg.enabled is True
            assert cfg.max_preview_chars == 500
            assert cfg.max_args_chars == 80
            assert cfg.warn_on_failure is False

    @pytest.mark.parametrize("raw,expected", [
        ("false", False), ("0", False), ("no", False), ("off", False),
        ("true", True), ("1", True), ("yes", True), ("on", True),
    ])
    def test_enabled_parsing(self, raw, expected):
        with patch.dict(os.environ, {"JARVIS_TOOL_NARRATION_ENABLED": raw}):
            assert NarrationConfig().enabled is expected

    def test_preview_chars_respects_env(self):
        with patch.dict(os.environ, {"JARVIS_TOOL_NARRATION_MAX_PREVIEW": "1200"}):
            assert NarrationConfig().max_preview_chars == 1200

    def test_preview_chars_invalid_falls_back_to_default(self):
        with patch.dict(os.environ, {"JARVIS_TOOL_NARRATION_MAX_PREVIEW": "not_a_number"}):
            assert NarrationConfig().max_preview_chars == 500

    def test_preview_chars_negative_clamps_to_zero(self):
        with patch.dict(os.environ, {"JARVIS_TOOL_NARRATION_MAX_PREVIEW": "-100"}):
            assert NarrationConfig().max_preview_chars == 0

    def test_warn_on_failure_env(self):
        with patch.dict(os.environ, {"JARVIS_TOOL_NARRATION_WARN": "true"}):
            assert NarrationConfig().warn_on_failure is True

    def test_config_is_frozen(self):
        cfg = NarrationConfig()
        with pytest.raises(Exception):
            cfg.enabled = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Core emit — happy path
# ---------------------------------------------------------------------------

class TestEmitHappyPath:
    @pytest.mark.asyncio
    async def test_start_event_marks_tool_starting(self):
        coll = _CollectingTransport()
        ch = ToolNarrationChannel(_DuckComm([coll]))
        ch.emit(op_id="op-1", tool_name="read_file", round_index=0, args_summary="foo.py")
        await asyncio.sleep(0)  # let create_task run
        assert len(coll.sent) == 1
        m = coll.sent[0]
        assert m.payload["status"] == "start"
        assert m.payload["tool_starting"] is True
        assert m.payload["tool_name"] == "read_file"
        assert m.payload["tool_args_summary"] == "foo.py"
        assert m.payload["phase"] == "generate"
        assert m.msg_type.value == "HEARTBEAT"

    @pytest.mark.asyncio
    async def test_start_event_carries_preamble(self):
        coll = _CollectingTransport()
        ch = ToolNarrationChannel(_DuckComm([coll]))
        ch.emit(
            op_id="op-1",
            tool_name="read_file",
            round_index=0,
            args_summary="foo.py",
            preamble="Inspecting the current implementation before editing.",
        )
        await asyncio.sleep(0)
        assert coll.sent[0].payload["preamble"] == "Inspecting the current implementation before editing."

    @pytest.mark.asyncio
    async def test_success_event_carries_result_preview(self):
        coll = _CollectingTransport()
        ch = ToolNarrationChannel(_DuckComm([coll]))
        ch.emit(
            op_id="op-2", tool_name="bash", round_index=2,
            args_summary="ls -la", result_preview="total 42\n…",
            duration_ms=123.4, status="success",
        )
        await asyncio.sleep(0)
        m = coll.sent[0]
        assert m.payload["status"] == "success"
        assert m.payload["tool_starting"] is False
        assert m.payload["duration_ms"] == 123.4
        assert m.payload["result_preview"].startswith("total 42")
        assert m.payload["preamble"] == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", sorted(LIFECYCLE_STATUSES))
    async def test_all_lifecycle_statuses_pass_through(self, status):
        coll = _CollectingTransport()
        ch = ToolNarrationChannel(_DuckComm([coll]))
        ch.emit(op_id="op-s", tool_name="edit_file", round_index=1, status=status)
        await asyncio.sleep(0)
        assert coll.sent[0].payload["status"] == status
        assert coll.sent[0].payload["tool_starting"] == (status == "start")

    @pytest.mark.asyncio
    async def test_empty_status_normalises_to_start(self):
        coll = _CollectingTransport()
        ch = ToolNarrationChannel(_DuckComm([coll]))
        ch.emit(op_id="op-e", tool_name="search_code", round_index=0, status="")
        await asyncio.sleep(0)
        assert coll.sent[0].payload["status"] == "start"

    @pytest.mark.asyncio
    async def test_all_payload_keys_present(self):
        coll = _CollectingTransport()
        ch = ToolNarrationChannel(_DuckComm([coll]))
        ch.emit(
            op_id="op-k", tool_name="run_tests", round_index=3,
            args_summary="pytest -x", result_preview="1 passed",
            duration_ms=99.0, status="success",
        )
        await asyncio.sleep(0)
        payload = coll.sent[0].payload
        for k in PAYLOAD_KEYS:
            assert k in payload, f"missing key {k}"

    @pytest.mark.asyncio
    async def test_parallel_tools_each_get_own_seq(self):
        coll = _CollectingTransport()
        ch = ToolNarrationChannel(_DuckComm([coll]))
        ch.emit(op_id="op-p", tool_name="read_file", round_index=0, args_summary="a.py")
        ch.emit(op_id="op-p", tool_name="read_file", round_index=0, args_summary="b.py")
        ch.emit(op_id="op-p", tool_name="read_file", round_index=0, args_summary="c.py")
        await asyncio.sleep(0)
        assert len(coll.sent) == 3
        # When CommMessage is available, seqs are monotonic per-channel.
        seqs = [getattr(m, "seq", -1) for m in coll.sent]
        if all(s >= 0 for s in seqs):
            assert seqs == sorted(seqs)
            assert len(set(seqs)) == 3


# ---------------------------------------------------------------------------
# Kill switch & guards
# ---------------------------------------------------------------------------

class TestGuards:
    @pytest.mark.asyncio
    async def test_disabled_is_noop(self):
        coll = _CollectingTransport()
        ch = ToolNarrationChannel(
            _DuckComm([coll]),
            config=NarrationConfig(enabled=False),
        )
        ch.emit(op_id="op-x", tool_name="read_file", round_index=0)
        await asyncio.sleep(0)
        assert coll.sent == []
        assert ch.emit_count == 0

    @pytest.mark.asyncio
    async def test_empty_tool_name_dropped(self):
        coll = _CollectingTransport()
        ch = ToolNarrationChannel(_DuckComm([coll]))
        ch.emit(op_id="op-x", tool_name="", round_index=0)
        await asyncio.sleep(0)
        assert coll.sent == []
        assert ch.emit_count == 0

    @pytest.mark.asyncio
    async def test_none_comm_is_noop(self):
        ch = ToolNarrationChannel(None)
        ch.emit(op_id="op-x", tool_name="read_file", round_index=0)
        await asyncio.sleep(0)
        assert ch.emit_count == 1
        assert ch.failure_count == 0


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

class TestTruncation:
    @pytest.mark.asyncio
    async def test_args_summary_truncated(self):
        coll = _CollectingTransport()
        ch = ToolNarrationChannel(
            _DuckComm([coll]),
            config=NarrationConfig(max_args_chars=10),
        )
        ch.emit(
            op_id="op-a", tool_name="bash", round_index=0,
            args_summary="a" * 100,
        )
        await asyncio.sleep(0)
        assert len(coll.sent[0].payload["tool_args_summary"]) == 10

    @pytest.mark.asyncio
    async def test_result_preview_truncated(self):
        coll = _CollectingTransport()
        ch = ToolNarrationChannel(
            _DuckComm([coll]),
            config=NarrationConfig(max_preview_chars=20),
        )
        ch.emit(
            op_id="op-p", tool_name="bash", round_index=0,
            result_preview="x" * 200, status="success",
        )
        await asyncio.sleep(0)
        assert len(coll.sent[0].payload["result_preview"]) == 20

    @pytest.mark.asyncio
    async def test_zero_max_chars_disables_truncation(self):
        coll = _CollectingTransport()
        ch = ToolNarrationChannel(
            _DuckComm([coll]),
            config=NarrationConfig(max_preview_chars=0, max_args_chars=0),
        )
        long = "y" * 5000
        ch.emit(
            op_id="op-z", tool_name="bash", round_index=0,
            args_summary=long, result_preview=long, status="success",
        )
        await asyncio.sleep(0)
        assert coll.sent[0].payload["tool_args_summary"] == long
        assert coll.sent[0].payload["result_preview"] == long


# ---------------------------------------------------------------------------
# Fault isolation
# ---------------------------------------------------------------------------

class TestFaultIsolation:
    @pytest.mark.asyncio
    async def test_faulty_transport_records_failure(self):
        faulty = _FaultyTransport()
        ch = ToolNarrationChannel(_DuckComm([faulty]))
        ch.emit(op_id="op-f", tool_name="bash", round_index=0)
        await asyncio.sleep(0)
        assert faulty.attempts == 1
        assert ch.failure_count == 1
        # Channel still advances emit_count regardless.
        assert ch.emit_count == 1

    @pytest.mark.asyncio
    async def test_faulty_and_good_transports_both_attempted(self):
        good = _CollectingTransport()
        faulty = _FaultyTransport()
        ch = ToolNarrationChannel(_DuckComm([faulty, good]))
        ch.emit(op_id="op-m", tool_name="bash", round_index=0)
        await asyncio.sleep(0)
        assert faulty.attempts == 1
        assert len(good.sent) == 1
        assert ch.failure_count == 1

    @pytest.mark.asyncio
    async def test_uses_comm_emit_when_available(self):
        emitting = _EmittingComm([_CollectingTransport()])
        ch = ToolNarrationChannel(emitting)
        ch.emit(op_id="op-e", tool_name="bash", round_index=0)
        await asyncio.sleep(0)
        assert len(emitting.emitted) == 1
        assert emitting.emitted[0].payload["tool_name"] == "bash"

    def test_no_running_loop_drops_silently(self):
        """Calling emit() outside any loop must not raise."""
        coll = _CollectingTransport()
        ch = ToolNarrationChannel(_DuckComm([coll]))
        # No asyncio.run — there is no running loop. The channel should
        # log at DEBUG and return without raising.
        ch.emit(op_id="op-n", tool_name="bash", round_index=0)
        assert ch.emit_count == 1
        assert coll.sent == []


# ---------------------------------------------------------------------------
# _DuckMessage fallback
# ---------------------------------------------------------------------------

class TestDuckMessage:
    def test_duck_matches_serpent_transport_contract(self):
        m = _DuckMessage(op_id="op-d", payload={"tool_name": "bash"})
        assert m.op_id == "op-d"
        assert m.payload["tool_name"] == "bash"
        assert m.msg_type.value == "HEARTBEAT"


# ---------------------------------------------------------------------------
# build_args_summary helper
# ---------------------------------------------------------------------------

class TestBuildArgsSummary:
    def test_none_arguments(self):
        assert build_args_summary(None) == ""

    def test_empty_dict(self):
        assert build_args_summary({}) == ""

    def test_first_value_used(self):
        assert build_args_summary({"path": "foo.py", "unused": "x"}) == "foo.py"

    def test_truncation(self):
        assert build_args_summary({"path": "a" * 200}, max_chars=10) == "a" * 10

    def test_zero_max_chars_no_truncation(self):
        s = "b" * 300
        assert build_args_summary({"path": s}, max_chars=0) == s

    def test_none_value(self):
        assert build_args_summary({"path": None}) == ""

    def test_non_string_value_coerced(self):
        assert build_args_summary({"lines": 42}) == "42"
