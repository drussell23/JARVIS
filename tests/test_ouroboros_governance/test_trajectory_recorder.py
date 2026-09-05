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

import asyncio
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


# ---------------------------------------------------------------------------
# Expiry must be driven by WALL CLOCK, not by queue traffic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_flushes_without_any_further_queue_activity(
    rec: TrajectoryRecorder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect that produced 4 candidate sets and 0 recorded lines.

    One generation arrives, its op never terminates, and NOTHING else is
    ever queued. Expiry that only ran after a queue item would wait
    forever; the watchdog must write it on time.
    """
    monkeypatch.setenv("JARVIS_TRAJECTORY_RECORDER_TICK_S", "1")
    monkeypatch.setenv("JARVIS_TRAJECTORY_RECORDER_PENDING_TTL_S", "30")

    rec.record_generation(
        op_id="op-stranded",
        prompt="p",
        generation_result=_FakeGenerationResult(candidates=(_candidate(),)),
    )
    await rec.drain()
    assert _lines(rec.path) == [], "not due yet"

    # Age it past the TTL without touching the queue. `_pending` maps an
    # op to its LINEAGE of attempts, so each attempt is aged on its own
    # clock (a fresh retry must not be flushed with a stale sibling).
    for lineage in rec._pending.values():
        for gen in lineage:
            gen.created_monotonic = 0.0

    for _ in range(60):
        if _lines(rec.path):
            break
        await asyncio.sleep(0.1)

    rows = _lines(rec.path)
    assert rows, "watchdog never flushed the stranded generation"
    assert rows[0]["outcome"] == "unknown"
    assert rows[0]["metadata"]["should_train"] is False
    assert rec.stats()["pending_expired"] == 1


@pytest.mark.asyncio
async def test_aclose_flushes_inflight_pendings(
    rec: TrajectoryRecorder,
) -> None:
    """A clean shutdown must not silently discard in-flight trajectories."""
    rec.record_generation(
        op_id="op-shutdown",
        prompt="p",
        generation_result=_FakeGenerationResult(candidates=(_candidate(),)),
    )
    await rec.drain()
    assert _lines(rec.path) == []

    await rec.aclose()
    rows = _lines(rec.path)
    assert len(rows) == 1
    assert rows[0]["metadata"]["op_id"] == "op-shutdown"


# ---------------------------------------------------------------------------
# Throughput must be recoverable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tokens_are_split_so_tok_s_is_derivable(
    rec: TrajectoryRecorder,
) -> None:
    """A summed tokens_used makes throughput unrecoverable, and throughput
    is half the model question."""
    gr = _FakeGenerationResult(
        candidates=(_candidate(),),
        total_input_tokens=900,
        total_output_tokens=300,
    )
    rec.record_generation(
        op_id="op-tok", prompt="p", generation_result=gr, latency_ms=2000.0,
    )
    rec.record_outcome(op_id="op-tok", terminal_reason="applied")
    await rec.drain()

    row = _lines(rec.path)[0]
    assert row["prompt_tokens"] == 900
    assert row["completion_tokens"] == 300
    assert row["tokens_used"] == 1200          # canonical field preserved
    assert row["tokens_per_second"] == 150.0   # 300 tok / 2.0 s


@pytest.mark.asyncio
async def test_model_id_override_wins_over_a_placeholder(
    rec: TrajectoryRecorder,
) -> None:
    """The local lane's GenerationResult reports model_id='gpt-4' -- the
    OpenAI-compat default, not the model that ran. In a MODEL A/B that
    field IS the experiment: three runs all labelled 'gpt-4' are
    indistinguishable and the corpus is worthless."""
    gr = _FakeGenerationResult(candidates=(_candidate(),), model_id="gpt-4")
    rec.record_generation(
        op_id="op-mid", prompt="p", generation_result=gr,
        model_id_override="qwen3.8:27b",
        completion_tokens_override=250,
        latency_ms=1000.0,
    )
    rec.record_outcome(op_id="op-mid", terminal_reason="applied")
    await rec.drain()

    row = _lines(rec.path)[0]
    assert row["model_id"] == "qwen3.8:27b"
    assert row["completion_tokens"] == 250
    assert row["tokens_per_second"] == 250.0


@pytest.mark.asyncio
async def test_absent_override_keeps_the_result_value(
    rec: TrajectoryRecorder,
) -> None:
    """A negative override means 'I don't know', not 'zero'."""
    gr = _FakeGenerationResult(
        candidates=(_candidate(),), total_output_tokens=77,
    )
    rec.record_generation(op_id="op-noov", prompt="p", generation_result=gr)
    rec.record_outcome(op_id="op-noov", terminal_reason="applied")
    await rec.drain()

    row = _lines(rec.path)[0]
    assert row["model_id"] == "qwen2.5-coder:32b"
    assert row["completion_tokens"] == 77


@pytest.mark.asyncio
async def test_zero_latency_does_not_divide_by_zero(
    rec: TrajectoryRecorder,
) -> None:
    rec.record_generation(
        op_id="op-zero", prompt="p",
        generation_result=_FakeGenerationResult(candidates=(_candidate(),)),
        latency_ms=0.0,
    )
    rec.record_outcome(op_id="op-zero", terminal_reason="applied")
    await rec.drain()
    assert _lines(rec.path)[0]["tokens_per_second"] == 0.0


# ---------------------------------------------------------------------------
# A broken join must be visible, not silent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_verdict_names_the_op(
    rec: TrajectoryRecorder, caplog: pytest.LogCaptureFixture,
) -> None:
    """If generation-side and terminal-side op_ids disagree, every
    trajectory is lost. The log is the only thing that can say so."""
    with caplog.at_level("INFO"):
        rec.record_outcome(op_id="op-mismatched", terminal_reason="applied")
        await rec.drain()

    assert rec.stats()["orphan_outcomes"] == 1
    assert any(
        "op-mismatched" in r.getMessage() for r in caplog.records
    ), "the orphaned op_id must appear in the log"


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


# ---------------------------------------------------------------------------
# A refusal is an answer: it gets a row (soak 19: 8 of 15 singletons)
# ---------------------------------------------------------------------------


def _noop_result(reason: str = "already fully implemented"):
    """What the provider builds for `2b.1-noop`: no candidates, is_noop,
    and the model's stated reason carried alongside."""
    r = _FakeGenerationResult(candidates=(), is_noop=True)
    r.noop_reason = reason  # type: ignore[attr-defined]
    return r


@pytest.mark.asyncio
async def test_a_refusal_is_persisted_as_a_row(rec: TrajectoryRecorder) -> None:
    """The whole change. Before, this returned False and wrote nothing:
    the model answered the prompt and the corpus kept no trace."""
    assert rec.record_generation(
        op_id="op-refuse", prompt="fix the recursion guard",
        generation_result=_noop_result("the guard is already present"),
    ) is True
    rec.record_outcome(op_id="op-refuse", terminal_reason="2b.1-noop")
    await rec.drain()

    rows = _lines(rec.path)
    assert len(rows) == 1
    row = rows[0]
    assert row["metadata"]["candidate_status"] == "noop"
    assert row["metadata"]["should_train"] is True
    assert row["outcome"] == "partial"


@pytest.mark.asyncio
async def test_the_body_is_the_envelope_not_the_prose(
    rec: TrajectoryRecorder,
) -> None:
    """Load-bearing: reactor's grader reads the ENVELOPE and scores it at
    the syntax ceiling. Bare prose fails json.loads, falls through to the
    source grader and is scored as BROKEN PYTHON -- teaching the model
    that declining is no better than emitting garbage."""
    rec.record_generation(
        op_id="op-env", prompt="p",
        generation_result=_noop_result("already correct"),
    )
    rec.record_outcome(op_id="op-env", terminal_reason="2b.1-noop")
    await rec.drain()

    body = _lines(rec.path)[0]["assistant_output"]
    parsed = json.loads(body)  # must be a real envelope
    assert parsed["schema_version"] == "2b.1-noop"
    assert parsed["reason"] == "already correct"


@pytest.mark.asyncio
async def test_an_empty_non_noop_result_is_still_dropped(
    rec: TrajectoryRecorder,
) -> None:
    """The old guard survives for what it was actually protecting: an
    empty result that is NOT a refusal is a fault, not an answer."""
    assert rec.record_generation(
        op_id="op-empty2", prompt="p",
        generation_result=_FakeGenerationResult(candidates=()),
    ) is False
    await rec.drain()
    assert _lines(rec.path) == []


@pytest.mark.asyncio
async def test_two_different_refusals_are_two_rows(
    rec: TrajectoryRecorder,
) -> None:
    """Different reasoning is a different answer, so the (op, hash) dedupe
    must not collapse them."""
    rec.record_generation(op_id="op-two", prompt="p",
                          generation_result=_noop_result("reason A"))
    rec.record_generation(op_id="op-two", prompt="p",
                          generation_result=_noop_result("reason B"))
    rec.record_outcome(op_id="op-two", terminal_reason="2b.1-noop")
    await rec.drain()

    rows = _lines(rec.path)
    assert len(rows) == 2
    hashes = {r["metadata"]["candidate_hash"] for r in rows}
    assert len(hashes) == 2


@pytest.mark.asyncio
async def test_a_repeated_refusal_is_deduped(rec: TrajectoryRecorder) -> None:
    """Saying the same thing twice is one answer given twice -- the
    existing (op_id, candidate_hash) dedupe must absorb it, so a model
    that noop-spams cannot flood the corpus with identical rows."""
    for _ in range(4):
        rec.record_generation(op_id="op-spam", prompt="p",
                              generation_result=_noop_result("same reason"))
    rec.record_outcome(op_id="op-spam", terminal_reason="2b.1-noop")
    await rec.drain()
    assert len(_lines(rec.path)) == 1


@pytest.mark.asyncio
async def test_refusal_and_patch_are_two_distinct_structures(
    rec: TrajectoryRecorder,
) -> None:
    """The pair this whole change exists to create.

    A refusal has no AST, so without its own structure class it would
    contribute nothing and a {refusal, patch} group would report
    n_distinct_structures=1 -- reading as unpairable when it is the
    widest-separated pair in the corpus.
    """
    rec.record_generation(op_id="op-mix", prompt="p",
                          generation_result=_noop_result("declining"))
    rec.record_generation(
        op_id="op-mix", prompt="p",
        generation_result=_FakeGenerationResult(candidates=(_candidate(),)),
    )
    rec.record_outcome(op_id="op-mix", terminal_reason="applied")
    await rec.drain()

    rows = _lines(rec.path)
    assert len(rows) == 2
    assert {r["metadata"]["candidate_status"] for r in rows} == {"noop", "patch"}
    # `n_distinct_structures` is stamped PER GENERATION and every local
    # draw is its own generation, so it reads 1 on both rows by
    # construction. The key that groups ACROSS draws is `structure_id`.
    sids = {r["metadata"]["structure_id"] for r in rows}
    assert len(sids) == 2, sids
    assert "noop" in sids


@pytest.mark.asyncio
async def test_all_refusal_group_reports_one_structure(
    rec: TrajectoryRecorder,
) -> None:
    """Declining twice is ONE answer given twice. The group must report a
    single structure so it stays out of the pairable count and, downstream,
    out of advantage normalisation."""
    rec.record_generation(op_id="op-allnoop", prompt="p",
                          generation_result=_noop_result("reason A"))
    rec.record_generation(op_id="op-allnoop", prompt="p",
                          generation_result=_noop_result("reason B"))
    rec.record_outcome(op_id="op-allnoop", terminal_reason="2b.1-noop")
    await rec.drain()

    rows = _lines(rec.path)
    assert len(rows) == 2
    # Two refusals are ONE answer given twice: a single structure class,
    # so the group stays out of the pairable count.
    assert {r["metadata"]["structure_id"] for r in rows} == {"noop"}


@pytest.mark.asyncio
async def test_a_patch_row_is_tagged_patch(rec: TrajectoryRecorder) -> None:
    """The tag is deterministic on BOTH sides -- a consumer never has to
    infer 'not a refusal' from an absent field."""
    rec.record_generation(
        op_id="op-patch", prompt="p",
        generation_result=_FakeGenerationResult(candidates=(_candidate(),)),
    )
    rec.record_outcome(op_id="op-patch", terminal_reason="applied")
    await rec.drain()
    assert _lines(rec.path)[0]["metadata"]["candidate_status"] == "patch"


@pytest.mark.asyncio
async def test_a_refusal_row_keeps_the_dpo_contract(
    rec: TrajectoryRecorder,
) -> None:
    """A synthesised row is still a row: every key the pair generator
    reads must be present, or the corpus silently stops pairing."""
    rec.record_generation(op_id="op-contract", prompt="p",
                          generation_result=_noop_result("nope"))
    rec.record_outcome(op_id="op-contract", terminal_reason="2b.1-noop")
    await rec.drain()
    row = _lines(rec.path)[0]
    assert _DPO_CONTRACT_KEYS.issubset(row.keys())
    assert row["event_type"] == "interaction"
    assert row["metadata"]["draw_kind"] == "primary"


def test_noop_candidate_is_deterministic() -> None:
    """Same refusal -> same hash, in-process and across runs. The dedupe
    and the retract seam both key on it."""
    from backend.core.ouroboros.governance.observability.trajectory_recorder import (  # noqa: E501
        noop_candidate,
    )

    a = noop_candidate("identical")
    b = noop_candidate("identical")
    c = noop_candidate("different")
    assert a["candidate_hash"] == b["candidate_hash"]
    assert a["candidate_hash"] != c["candidate_hash"]
    assert a["candidate_status"] == "noop"
    assert json.loads(a["full_content"])["schema_version"] == "2b.1-noop"


def test_noop_candidate_survives_a_missing_reason() -> None:
    """A provider that declined without saying why still yields a valid,
    gradable envelope rather than an empty body the writer would skip."""
    from backend.core.ouroboros.governance.observability.trajectory_recorder import (  # noqa: E501
        noop_candidate,
    )

    cand = noop_candidate("")
    assert cand["full_content"]
    assert json.loads(cand["full_content"])["reason"] == ""


@pytest.mark.asyncio
async def test_harvest_counts_a_refusal_as_its_own_answer(
    rec: TrajectoryRecorder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The diagnostic must not deny the pair the corpus just gained.

    `_render_groups` recomputes a fingerprint from `assistant_output`; a
    refusal's body is an envelope, not Python, so it fingerprints as None
    and a {refusal, patch} group would read `collapsed` -- the exact
    "healthy row count, zero pairs" blindness /harvest exists to expose.
    """
    from backend.core.ouroboros.governance import harvest_repl

    rec.record_generation(op_id="op-hv", prompt="p",
                          generation_result=_noop_result("declining"))
    rec.record_generation(
        op_id="op-hv", prompt="p",
        generation_result=_FakeGenerationResult(candidates=(_candidate(),)),
    )
    rec.record_outcome(op_id="op-hv", terminal_reason="applied")
    await rec.drain()

    monkeypatch.setenv("JARVIS_TRAJECTORY_RECORDER_DIR", str(rec.path.parent))
    text = harvest_repl._render_groups(10)
    assert "op-hv" in text
    line = next(ln for ln in text.splitlines() if "op-hv" in ln)
    assert "PAIRABLE" in line, line


# ---------------------------------------------------------------------------
# An unparseable draw is an answer too (soak 19: 2 of 15 singletons)
# ---------------------------------------------------------------------------


_RAW_BAD = (
    '{"schema_version":"2b.1","candidates":[{"candidate_id":"c1",'
    '"file_path":"m.py","full_content":"def broken(:\\n    return 1\\n",'
    '"rationale":"attempt"}]}'
)


def test_parse_error_candidate_keeps_the_raw_body() -> None:
    """The body must be the model's RAW response, verbatim.

    reactor's grader reaches through the envelope to the invalid Python and
    scores it by how far the parse got (0.250 line-1/3, 0.393 line-6/7).
    A summary, a marker, or the exception's `candidate_preview` (raw[:800])
    would throw that gradient away — and the preview would fabricate a
    truncation the model never emitted.
    """
    from backend.core.ouroboros.governance.observability.trajectory_recorder import (  # noqa: E501
        parse_error_candidate,
    )

    cand = parse_error_candidate(_RAW_BAD, [{"file_path": "m.py", "line": 1}])
    assert cand["full_content"] == _RAW_BAD
    assert cand["candidate_status"] == "parse_error"
    assert cand["file_path"] == "m.py"
    # deterministic + distinguishing
    assert cand["candidate_hash"] == parse_error_candidate(_RAW_BAD)["candidate_hash"]
    assert cand["candidate_hash"] != parse_error_candidate("other")["candidate_hash"]


def test_parse_error_candidate_survives_empty_input() -> None:
    from backend.core.ouroboros.governance.observability.trajectory_recorder import (  # noqa: E501
        parse_error_candidate,
    )

    cand = parse_error_candidate("", None)
    assert cand["candidate_status"] == "parse_error"
    assert cand["candidate_hash"]


@pytest.mark.asyncio
async def test_a_parse_error_row_is_persisted(rec: TrajectoryRecorder) -> None:
    from backend.core.ouroboros.governance.observability.trajectory_recorder import (  # noqa: E501
        parse_error_candidate,
    )

    rec.record_generation(
        op_id="op-pe", prompt="p",
        generation_result=_FakeGenerationResult(
            candidates=(parse_error_candidate(_RAW_BAD),),
        ),
    )
    rec.record_outcome(op_id="op-pe", terminal_reason="all_candidates_syntax_error")
    await rec.drain()

    row = _lines(rec.path)[0]
    assert row["metadata"]["candidate_status"] == "parse_error"
    # `all_candidates_syntax_error` is the trainable failure, by policy.
    assert row["outcome"] == "failure"
    assert row["metadata"]["should_train"] is True
    assert row["assistant_output"] == _RAW_BAD


@pytest.mark.asyncio
async def test_parse_error_is_its_own_structure_class(
    rec: TrajectoryRecorder,
) -> None:
    """Three answer kinds, three classes: a parse error is neither a
    refusal nor any patch, and two parse errors are one class."""
    from backend.core.ouroboros.governance.observability.trajectory_recorder import (  # noqa: E501
        parse_error_candidate,
    )

    rec.record_generation(
        op_id="op-3way", prompt="p",
        generation_result=_FakeGenerationResult(
            candidates=(parse_error_candidate(_RAW_BAD),),
        ),
    )
    rec.record_generation(op_id="op-3way", prompt="p",
                          generation_result=_noop_result("declining"))
    rec.record_generation(
        op_id="op-3way", prompt="p",
        generation_result=_FakeGenerationResult(candidates=(_candidate(),)),
    )
    rec.record_outcome(op_id="op-3way", terminal_reason="applied")
    await rec.drain()

    rows = _lines(rec.path)
    assert len(rows) == 3
    sids = {r["metadata"]["structure_id"] for r in rows}
    assert len(sids) == 3, sids
    assert {"noop", "parse_error"} <= sids
    assert {r["metadata"]["candidate_status"] for r in rows} == {
        "noop", "parse_error", "patch",
    }

# ---------------------------------------------------------------------------
# A refusal is self-evidencing: it does not need the op's verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noop_is_trainable_even_when_the_op_never_reported(
    rec: TrajectoryRecorder,
) -> None:
    """The measured loss: 70 noop/primary rows written, 24 survived.

    An op that declines never reaches a verdict-bearing phase, so
    classify_terminal_reason falls through to _UNKNOWN (should_train
    False) and the refusal is discarded downstream. But the refusal's
    outcome is not unseen — the generation declared it.
    """
    rec.record_generation(
        op_id="op-unvouched", prompt="p",
        generation_result=_noop_result("already correct"),
    )
    # a reason the policy does not name -> _UNKNOWN
    rec.record_outcome(op_id="op-unvouched", terminal_reason="")
    await rec.drain()

    row = _lines(rec.path)[0]
    assert row["outcome"] == "partial"
    assert row["metadata"]["should_train"] is True
    assert row["metadata"]["candidate_status"] == "noop"


@pytest.mark.asyncio
async def test_a_judged_failure_still_outranks_the_declaration(
    rec: TrajectoryRecorder,
) -> None:
    """`failure` is NOT overridable: a candidate judged bad on its own
    merits outranks what the generation declared about itself."""
    rec.record_generation(
        op_id="op-judged", prompt="p",
        generation_result=_noop_result("claims nothing to do"),
    )
    rec.record_outcome(op_id="op-judged", terminal_reason="validation_failed")
    await rec.drain()

    row = _lines(rec.path)[0]
    assert row["outcome"] == "failure"
    assert row["metadata"]["should_train"] is True


@pytest.mark.asyncio
async def test_a_caged_noop_stays_untrainable(rec: TrajectoryRecorder) -> None:
    """Governance denials are never model quality — the cage outcome must
    survive the refusal override, or the corpus learns from ops whose
    death the model did not cause."""
    rec.record_generation(
        op_id="op-caged", prompt="p",
        generation_result=_noop_result("declining"),
    )
    rec.record_outcome(
        op_id="op-caged", terminal_reason="self_modification_unsanctioned_source",
    )
    await rec.drain()

    row = _lines(rec.path)[0]
    assert row["metadata"]["should_train"] is False


@pytest.mark.asyncio
async def test_a_patch_with_no_verdict_is_still_untrainable(
    rec: TrajectoryRecorder,
) -> None:
    """The override is for REFUSALS only. A patch whose op never reported
    is genuinely unseen — nothing declares whether the code was good."""
    rec.record_generation(
        op_id="op-patch-unvouched", prompt="p",
        generation_result=_FakeGenerationResult(candidates=(_candidate(),)),
    )
    rec.record_outcome(op_id="op-patch-unvouched", terminal_reason="")
    await rec.drain()

    row = _lines(rec.path)[0]
    assert row["metadata"]["should_train"] is False

@pytest.mark.asyncio
async def test_a_refusal_that_EXPIRES_is_still_trainable(
    rec: TrajectoryRecorder,
) -> None:
    """The second write path. An op that declines often never reports at
    all, so its row is written by the pending-EXPIRY sweep — which
    hardcoded (unknown, intent_written, False) and discarded the refusal
    even after the verdict-joined path was fixed. 6 of soak 24's first 40
    rows died here.
    """
    rec.record_generation(
        op_id="op-expires", prompt="p",
        generation_result=_noop_result("nothing to change"),
    )
    await rec.drain()
    # TTL is floored at 30s, so age the entry rather than shrink the TTL --
    # the same move aclose() makes for its final flush.
    for lineage in rec._pending.values():
        for gen in lineage:
            gen.created_monotonic = 0.0
    await rec._expire_pending()
    await rec.drain()

    rows = [r for r in _lines(rec.path)
            if r["metadata"]["op_id"] == "op-expires"]
    assert rows, "the expiry sweep wrote no row at all"
    row = rows[0]
    assert row["metadata"]["candidate_status"] == "noop"
    assert row["outcome"] == "partial"
    assert row["metadata"]["should_train"] is True


@pytest.mark.asyncio
async def test_a_PATCH_that_expires_stays_untrainable(
    rec: TrajectoryRecorder,
) -> None:
    """The override must not leak to patches. A patch whose op never
    reported is genuinely unlabelled — nothing declares whether its code
    was correct."""
    rec.record_generation(
        op_id="op-patch-expires", prompt="p",
        generation_result=_FakeGenerationResult(candidates=(_candidate(),)),
    )
    await rec.drain()
    for lineage in rec._pending.values():
        for gen in lineage:
            gen.created_monotonic = 0.0
    await rec._expire_pending()
    await rec.drain()

    rows = [r for r in _lines(rec.path)
            if r["metadata"]["op_id"] == "op-patch-expires"]
    assert rows
    assert rows[0]["metadata"]["should_train"] is False
