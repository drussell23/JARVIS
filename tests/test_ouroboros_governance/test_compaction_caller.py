"""
Tests for CompactionCaller — Functions-not-Agents Phase 0.

Covers:
  1. CompactionCallerConfig.from_env default (disabled)
  2. Anti-hallucination gate: clean accept
  3. Anti-hallucination gate: hallucinated key
  4. Anti-hallucination gate: hallucinated phase
  5. Anti-hallucination gate: empty / malformed / oversized
  6. Strategy disabled short-circuit (no network)
  7. Strategy circuit-breaker opens after N failures

No live DW calls — all provider behavior is stubbed via ``FakeProvider``.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from backend.core.ouroboros.governance.compaction_caller import (
    CompactionCallerConfig,
    CompactionCallerStrategy,
    _parse_and_validate,
    reset_session_state,
    _SESSION_STATE,
)
from backend.core.ouroboros.governance.doubleword_provider import CompleteSyncResult


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_breaker():
    reset_session_state()
    yield
    reset_session_state()


@pytest.fixture
def _live_env(monkeypatch):
    """Enable the caller in LIVE mode for direct summary-return testing."""
    monkeypatch.setenv("JARVIS_COMPACTION_CALLER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_COMPACTION_CALLER_MODE", "live")
    monkeypatch.setenv("JARVIS_COMPACTION_CALLER_TIMEOUT_S", "1.0")
    monkeypatch.setenv("JARVIS_COMPACTION_CALLER_MAX_FAILURES", "3")
    yield


@pytest.fixture
def _shadow_env(monkeypatch):
    monkeypatch.setenv("JARVIS_COMPACTION_CALLER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_COMPACTION_CALLER_MODE", "shadow")
    monkeypatch.setenv("JARVIS_COMPACTION_CALLER_TIMEOUT_S", "1.0")
    yield


_ENTRIES: List[Dict[str, Any]] = [
    {"op_id": "op-001", "phase": "GENERATE", "type": "model_call"},
    {"op_id": "op-002", "phase": "VALIDATE", "type": "iron_gate"},
    {"op_id": "op-003", "phase": "APPLY", "type": "change_engine"},
]


class FakeProvider:
    """Stub DoublewordProvider that returns a caller-supplied payload."""

    def __init__(
        self,
        *,
        response_content: Optional[str] = None,
        raise_exc: Optional[BaseException] = None,
        latency_s: float = 0.12,
    ) -> None:
        self._response_content = response_content
        self._raise_exc = raise_exc
        self._latency_s = latency_s
        self.calls: List[Dict[str, Any]] = []

    async def complete_sync(
        self,
        *,
        prompt: str,
        system_prompt: str,
        caller_id: str,
        model: Optional[str] = None,
        max_tokens: int = 512,
        timeout_s: float = 10.0,
        response_format: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
    ) -> CompleteSyncResult:
        self.calls.append({"prompt": prompt, "caller": caller_id})
        if self._raise_exc is not None:
            raise self._raise_exc
        return CompleteSyncResult(
            content=self._response_content or "",
            input_tokens=50,
            output_tokens=20,
            cost_usd=0.00005,
            latency_s=self._latency_s,
            model=model or "google/gemma-4-31B-it",
        )


# ---------------------------------------------------------------------------
# 1. Config default
# ---------------------------------------------------------------------------


def test_config_default_is_disabled(monkeypatch):
    """from_env with no vars set → enabled=False, mode=disabled."""
    monkeypatch.delenv("JARVIS_COMPACTION_CALLER_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_COMPACTION_CALLER_MODE", raising=False)
    cfg = CompactionCallerConfig.from_env()
    assert cfg.enabled is False
    assert cfg.mode == "disabled"


def test_config_shadow_mode_when_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_COMPACTION_CALLER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_COMPACTION_CALLER_MODE", "shadow")
    cfg = CompactionCallerConfig.from_env()
    assert cfg.enabled is True
    assert cfg.mode == "shadow"


# ---------------------------------------------------------------------------
# 2. Anti-hallucination gate — direct function tests
# ---------------------------------------------------------------------------


def test_gate_accepts_valid_output():
    valid = json.dumps(
        {
            "summary": "3 model calls spanning GENERATE→VALIDATE→APPLY",
            "referenced_keys": ["op_id=op-001", "op_id=op-002"],
            "referenced_phases": ["GENERATE", "APPLY"],
        }
    )
    input_keys = {"op_id=op-001", "op_id=op-002", "op_id=op-003"}
    input_phases = {"GENERATE", "VALIDATE", "APPLY"}
    result = _parse_and_validate(
        raw_content=valid, input_keys=input_keys, input_phases=input_phases,
    )
    assert result.ok
    assert "GENERATE" in result.summary


def test_gate_rejects_hallucinated_key():
    bad = json.dumps(
        {
            "summary": "summary ok",
            "referenced_keys": ["op_id=op-999"],
            "referenced_phases": [],
        }
    )
    result = _parse_and_validate(
        raw_content=bad,
        input_keys={"op_id=op-001"},
        input_phases={"GENERATE"},
    )
    assert not result.ok
    assert result.reason is not None
    assert result.reason.startswith("hallucinated_key")


def test_gate_rejects_hallucinated_phase():
    bad = json.dumps(
        {
            "summary": "summary ok",
            "referenced_keys": [],
            "referenced_phases": ["FABRICATED_PHASE"],
        }
    )
    result = _parse_and_validate(
        raw_content=bad,
        input_keys={"op_id=op-001"},
        input_phases={"GENERATE"},
    )
    assert not result.ok
    assert result.reason is not None
    assert result.reason.startswith("hallucinated_phase")


def test_gate_rejects_missing_summary():
    bad = json.dumps({"referenced_keys": [], "referenced_phases": []})
    result = _parse_and_validate(
        raw_content=bad, input_keys=set(), input_phases=set(),
    )
    assert not result.ok
    assert result.reason == "missing_summary"


def test_gate_rejects_invalid_json():
    result = _parse_and_validate(
        raw_content="not a json object at all",
        input_keys=set(),
        input_phases=set(),
    )
    assert not result.ok
    assert result.reason is not None
    assert result.reason.startswith("json_decode")


def test_gate_rejects_empty_content():
    result = _parse_and_validate(
        raw_content="", input_keys=set(), input_phases=set(),
    )
    assert not result.ok
    assert result.reason == "empty_content"


def test_gate_rejects_oversized_summary():
    bad = json.dumps(
        {
            "summary": "x" * 900,
            "referenced_keys": [],
            "referenced_phases": [],
        }
    )
    result = _parse_and_validate(
        raw_content=bad, input_keys=set(), input_phases=set(),
    )
    assert not result.ok
    assert result.reason == "summary_too_long"


# ---------------------------------------------------------------------------
# 3. Strategy short-circuit when disabled
# ---------------------------------------------------------------------------


def test_strategy_disabled_skips_network(monkeypatch):
    monkeypatch.delenv("JARVIS_COMPACTION_CALLER_ENABLED", raising=False)
    provider = FakeProvider(response_content="{}")
    strategy = CompactionCallerStrategy(provider=provider)
    result = asyncio.get_event_loop().run_until_complete(
        strategy.summarize(_ENTRIES, deterministic_summary="det")
    )
    assert result.accepted is False
    assert result.rejection_reason == "disabled"
    assert len(provider.calls) == 0


# ---------------------------------------------------------------------------
# 4. Strategy full-path acceptance in LIVE mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategy_live_accept(_live_env, monkeypatch, tmp_path: Path):
    valid = json.dumps(
        {
            "summary": "compacted 3 entries across GENERATE/VALIDATE/APPLY",
            "referenced_keys": ["op_id=op-001"],
            "referenced_phases": ["GENERATE"],
        }
    )
    # Force topology to resolve the compaction caller model
    monkeypatch.setattr(
        CompactionCallerStrategy,
        "_resolve_model",
        lambda self: "google/gemma-4-31B-it",
    )
    provider = FakeProvider(response_content=valid)
    strategy = CompactionCallerStrategy(provider=provider, session_dir=tmp_path)
    assert strategy.enabled

    result = await strategy.summarize(_ENTRIES, deterministic_summary="det fallback")
    assert result.accepted is True
    assert result.summary is not None
    assert "compacted 3 entries" in result.summary
    assert len(provider.calls) == 1

    jsonl = tmp_path / "compaction_shadow.jsonl"
    assert jsonl.exists()
    record = json.loads(jsonl.read_text(encoding="utf-8").strip())
    assert record["accepted"] is True
    assert record["caller"] == "compaction"


# ---------------------------------------------------------------------------
# 5. Strategy shadow mode never returns summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategy_shadow_never_returns_summary(
    _shadow_env, monkeypatch, tmp_path: Path,
):
    valid = json.dumps(
        {
            "summary": "would be live but shadow suppresses return",
            "referenced_keys": [],
            "referenced_phases": [],
        }
    )
    monkeypatch.setattr(
        CompactionCallerStrategy,
        "_resolve_model",
        lambda self: "google/gemma-4-31B-it",
    )
    provider = FakeProvider(response_content=valid)
    strategy = CompactionCallerStrategy(provider=provider, session_dir=tmp_path)

    result = await strategy.summarize(_ENTRIES, deterministic_summary="det")
    assert result.accepted is True
    assert result.summary is None  # shadow mode suppresses


# ---------------------------------------------------------------------------
# 6. Circuit breaker opens after N consecutive failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategy_circuit_breaker_opens(_live_env, monkeypatch):
    monkeypatch.setattr(
        CompactionCallerStrategy,
        "_resolve_model",
        lambda self: "google/gemma-4-31B-it",
    )
    provider = FakeProvider(raise_exc=asyncio.TimeoutError())
    strategy = CompactionCallerStrategy(provider=provider)

    r1 = await strategy.summarize(_ENTRIES, "det")
    r2 = await strategy.summarize(_ENTRIES, "det")
    r3 = await strategy.summarize(_ENTRIES, "det")
    assert all(not r.accepted for r in (r1, r2, r3))
    assert _SESSION_STATE.breaker_open is True

    # 4th call: breaker is already open, no network call should happen
    initial_call_count = len(provider.calls)
    r4 = await strategy.summarize(_ENTRIES, "det")
    assert not r4.accepted
    assert r4.rejection_reason == "breaker_open"
    assert len(provider.calls) == initial_call_count  # no network call


# ---------------------------------------------------------------------------
# 7. Timeout is counted as a failure with specific reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategy_timeout_reason(_live_env, monkeypatch):
    monkeypatch.setattr(
        CompactionCallerStrategy,
        "_resolve_model",
        lambda self: "google/gemma-4-31B-it",
    )
    provider = FakeProvider(raise_exc=asyncio.TimeoutError())
    strategy = CompactionCallerStrategy(provider=provider)
    result = await strategy.summarize(_ENTRIES, "det")
    assert not result.accepted
    assert result.rejection_reason == "timeout"
