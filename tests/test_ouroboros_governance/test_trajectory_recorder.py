"""Tests for the O+V trajectory recorder (Reactor-Core data flywheel).

Covers the three properties the recorder MUST hold:
  1. Default-off is byte-identical silence (§33.1).
  2. A generation + its verdict join into canonical ExperienceEvent
     lines whose keys are exactly the ones Reactor-Core's DPO ingest
     reads — the cross-repo contract, asserted literally because the
     two repos cannot import each other.
  3. Governance denials are NOT trainable: the cage is infrastructure,
     never a model-quality signal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from backend.core.ouroboros.governance.observability.trajectory_recorder import (  # noqa: E501
    TrajectoryRecorder,
    classify_terminal_reason,
    events_dir,
    recorder_enabled,
    reset_recorder_for_tests,
)

_ENV_MASTER = "JARVIS_TRAJECTORY_RECORDER_ENABLED"

# The exact keys reactor_core/training/dpo_pair_generator.py reads first
# in _ingest_telemetry(). If this set drifts, the corpus silently stops
# producing pairs — so it is pinned here.
_DPO_CONTRACT_KEYS = {
    "user_input",
    "assistant_output",
    "model_id",
    "outcome",
    "confidence",
    "latency_ms",
    "timestamp",
    "event_id",
    "metadata",
}


@dataclass
class _FakeGenerationResult:
    """Minimal stand-in for GenerationResult (duck-typed by the cache
    projector, which only getattr()s these names)."""

    candidates: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    provider_name: str = "local-prime"
    model_id: str = "qwen2.5-coder:32b"
    is_noop: bool = False
    prompt_preloaded_files: Tuple[str, ...] = field(default_factory=tuple)
    total_input_tokens: int = 900
    total_output_tokens: int = 120
    cost_usd: float = 0.0


def _candidate(cid: str = "c1", body: str = "def f():\n    return 1\n") -> Dict[str, Any]:
    return {
        "candidate_id": cid,
        "candidate_hash": f"hash-{cid}",
        "file_path": "backend/mod.py",
        "source_path": "backend/mod.py",
        "full_content": body,
        "rationale": "unit-test candidate",
    }


@pytest.fixture()
def rec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TrajectoryRecorder:
    monkeypatch.setenv(_ENV_MASTER, "true")
    return reset_recorder_for_tests(path=tmp_path / "experience.jsonl")


def _lines(path: Path) -> list:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# 1. Default-off
# ---------------------------------------------------------------------------


def test_master_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV_MASTER, raising=False)
    assert recorder_enabled() is False


@pytest.mark.asyncio
async def test_disabled_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_ENV_MASTER, raising=False)
    r = reset_recorder_for_tests(path=tmp_path / "experience.jsonl")
    assert r.record_generation(
        op_id="op-1",
        prompt="fix it",
        generation_result=_FakeGenerationResult(candidates=(_candidate(),)),
    ) is False
    assert r.record_outcome(op_id="op-1", terminal_reason="applied") is False
    await r.aclose()
    assert _lines(tmp_path / "experience.jsonl") == []


def test_events_dir_defaults_to_trinity_watch_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The receiver watches ~/.jarvis/trinity/events — write there."""
    monkeypatch.delenv("JARVIS_TRAJECTORY_RECORDER_DIR", raising=False)
    assert events_dir().parts[-3:] == (".jarvis", "trinity", "events")


# ---------------------------------------------------------------------------
# 2. Join + canonical schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generation_joins_with_verdict(rec: TrajectoryRecorder) -> None:
    assert rec.record_generation(
        op_id="op-42",
        prompt="repair _should_use_lean_prompt",
        generation_result=_FakeGenerationResult(candidates=(_candidate(),)),
        latency_ms=8500.0,
        task_type="code_debug",
        session_id="bt-test",
    ) is True
    assert rec.record_outcome(
        op_id="op-42", terminal_phase="COMPLETED",
        terminal_reason="background_accepted",
    ) is True
    await rec.drain()

    rows = _lines(rec.path)
    assert len(rows) == 1
    row = rows[0]
    assert _DPO_CONTRACT_KEYS.issubset(row.keys())
    assert row["user_input"] == "repair _should_use_lean_prompt"
    assert row["assistant_output"] == "def f():\n    return 1\n"
    assert row["outcome"] == "success"
    assert row["model_id"] == "qwen2.5-coder:32b"
    assert row["latency_ms"] == 8500.0
    assert row["task_type"] == "code_debug"
    assert row["metadata"]["op_id"] == "op-42"
    assert row["metadata"]["should_train"] is True
    assert row["metadata"]["terminal_reason"] == "background_accepted"
    assert row["metadata"]["prompt_key"]  # stable grouping identity


@pytest.mark.asyncio
async def test_one_line_per_candidate(rec: TrajectoryRecorder) -> None:
    """DPO needs >=2 responses per prompt to form a pair, so every
    candidate must be its own row."""
    gr = _FakeGenerationResult(
        candidates=(
            _candidate("c1", "def f():\n    return 1\n"),
            _candidate("c2", "def f():\n    return 2\n"),
        )
    )
    rec.record_generation(op_id="op-2", prompt="same prompt", generation_result=gr)
    rec.record_outcome(op_id="op-2", terminal_reason="applied")
    await rec.drain()

    rows = _lines(rec.path)
    assert len(rows) == 2
    assert {r["metadata"]["candidate_index"] for r in rows} == {0, 1}
    assert {r["assistant_output"] for r in rows} == {
        "def f():\n    return 1\n",
        "def f():\n    return 2\n",
    }
    # Same prompt => same grouping key, which is what lets the DPO
    # generator put them in one comparison group.
    assert len({r["metadata"]["prompt_key"] for r in rows}) == 1


@pytest.mark.asyncio
async def test_syntax_error_is_a_trainable_failure(
    rec: TrajectoryRecorder,
) -> None:
    rec.record_generation(
        op_id="op-3",
        prompt="p",
        generation_result=_FakeGenerationResult(candidates=(_candidate(),)),
    )
    rec.record_outcome(
        op_id="op-3", terminal_phase="GENERATE",
        terminal_reason="all_candidates_syntax_error",
    )
    await rec.drain()

    row = _lines(rec.path)[0]
    assert row["outcome"] == "failure"
    assert row["confidence"] == 0.0
    assert row["metadata"]["should_train"] is True
    assert row["metadata"]["autonomy_event_type"] == "failed"


# ---------------------------------------------------------------------------
# 3. The cage is not a quality signal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "self_modification_unsanctioned_source",
        "touches_kernel",
        "touches_supervisor",
        "touches_security",
        "target_out_of_scope",
    ],
)
@pytest.mark.asyncio
async def test_governance_denial_is_not_trainable(
    rec: TrajectoryRecorder, reason: str,
) -> None:
    rec.record_generation(
        op_id=f"op-{reason}",
        prompt="p",
        generation_result=_FakeGenerationResult(candidates=(_candidate(),)),
    )
    rec.record_outcome(op_id=f"op-{reason}", terminal_reason=reason)
    await rec.drain()

    row = _lines(rec.path)[-1]
    assert row["outcome"] == "unknown"
    assert row["metadata"]["should_train"] is False
    assert row["metadata"]["autonomy_event_type"] == "policy_denied"


def test_classifier_degrades_unknown_reasons_rather_than_guessing() -> None:
    outcome, autonomy, train = classify_terminal_reason("some_new_reason_code")
    assert (outcome, train) == ("unknown", False)
    assert autonomy == "intent_written"
    # ...but a terminal phase that unambiguously means "it landed" wins.
    assert classify_terminal_reason("", "COMPLETED")[0] == "success"


@pytest.mark.asyncio
async def test_noop_verdict_is_partial_not_success(
    rec: TrajectoryRecorder,
) -> None:
    """'2b.1-noop is an answer, not an absence' — but it is not an
    applied patch either."""
    rec.record_generation(
        op_id="op-noop",
        prompt="p",
        generation_result=_FakeGenerationResult(
            candidates=(_candidate(),), is_noop=True,
        ),
    )
    rec.record_outcome(op_id="op-noop", terminal_reason="2b.1-noop")
    await rec.drain()

    row = _lines(rec.path)[0]
    assert row["outcome"] == "partial"
    assert row["metadata"]["is_noop"] is True
    assert row["metadata"]["should_train"] is True


# ---------------------------------------------------------------------------
# Bounds / resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outcome_without_generation_writes_nothing(
    rec: TrajectoryRecorder,
) -> None:
    """An op caged before GENERATE has no candidate text to train on."""
    rec.record_outcome(op_id="op-never-generated", terminal_reason="applied")
    await rec.drain()
    assert _lines(rec.path) == []
    assert rec.stats()["orphan_outcomes"] == 1


@pytest.mark.asyncio
async def test_pending_map_is_bounded(
    rec: TrajectoryRecorder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TRAJECTORY_RECORDER_PENDING_MAX", "8")
    for i in range(20):
        rec.record_generation(
            op_id=f"op-{i}",
            prompt=f"p{i}",
            generation_result=_FakeGenerationResult(
                candidates=(_candidate(),),
            ),
        )
    await rec.drain()
    stats = rec.stats()
    assert stats["pending_open"] <= 8
    assert stats["pending_evicted"] >= 12


@pytest.mark.asyncio
async def test_empty_candidate_set_is_not_recorded(
    rec: TrajectoryRecorder,
) -> None:
    assert rec.record_generation(
        op_id="op-empty",
        prompt="p",
        generation_result=_FakeGenerationResult(candidates=()),
    ) is False


@pytest.mark.asyncio
async def test_prompt_and_output_are_capped(
    rec: TrajectoryRecorder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TRAJECTORY_RECORDER_MAX_PROMPT_CHARS", "256")
    monkeypatch.setenv("JARVIS_TRAJECTORY_RECORDER_MAX_OUTPUT_CHARS", "256")
    rec.record_generation(
        op_id="op-big",
        prompt="x" * 100_000,
        generation_result=_FakeGenerationResult(
            candidates=(_candidate(body="y" * 100_000),),
        ),
    )
    rec.record_outcome(op_id="op-big", terminal_reason="applied")
    await rec.drain()

    row = _lines(rec.path)[0]
    assert len(row["user_input"]) < 1_000
    assert len(row["assistant_output"]) < 1_000
    assert "truncated by trajectory_recorder" in row["assistant_output"]


def test_emit_without_running_loop_is_silent_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Called from sync code with no loop: drop and count, never raise."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    r = reset_recorder_for_tests(path=tmp_path / "experience.jsonl")
    assert r.record_generation(
        op_id="op-sync",
        prompt="p",
        generation_result=_FakeGenerationResult(candidates=(_candidate(),)),
    ) is False
    assert r.stats()["dropped_no_loop"] >= 1
