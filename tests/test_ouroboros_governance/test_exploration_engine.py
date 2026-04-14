"""Unit tests for ExplorationEngine (MVP — Task #102).

Covers ledger construction, diversity scoring, duplicate handling, failed
calls, env-driven floors, verdict evaluation, retry-feedback rendering,
and the feature-flag parser. Zero orchestrator wiring is tested here —
those tests live in the follow-up patch that integrates the ledger into
the Iron Gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from backend.core.ouroboros.governance.exploration_engine import (
    ExplorationCall,
    ExplorationCategory,
    ExplorationFloors,
    ExplorationLedger,
    ExplorationVerdict,
    evaluate_exploration,
    is_ledger_enabled,
    render_retry_feedback,
)


# ---------------------------------------------------------------------------
# Fake record — duck-typed stand-in for ToolExecutionRecord
# ---------------------------------------------------------------------------


@dataclass
class FakeRecord:
    tool_name: str
    arguments_hash: str = "h-default"
    output_bytes: int = 0
    status: Optional[str] = "success"


def _call(
    tool: str,
    args: str = "a",
    *,
    ok: bool = True,
    output_bytes: int = 0,
) -> ExplorationCall:
    return ExplorationCall(
        tool_name=tool,
        arguments_hash=args,
        output_bytes=output_bytes,
        succeeded=ok,
    )


# ---------------------------------------------------------------------------
# ExplorationCall — category + weight lookup
# ---------------------------------------------------------------------------


def test_call_known_tool_maps_to_category_and_weight() -> None:
    c = _call("get_callers")
    assert c.category is ExplorationCategory.CALL_GRAPH
    assert c.base_weight == 2.0


def test_call_unknown_tool_maps_to_uncategorized_and_zero_weight() -> None:
    c = _call("edit_file")  # mutator — must not be exploration
    assert c.category is ExplorationCategory.UNCATEGORIZED
    assert c.base_weight == 0.0


# ---------------------------------------------------------------------------
# ExplorationLedger.diversity_score
# ---------------------------------------------------------------------------


def test_score_sums_distinct_calls_by_base_weight() -> None:
    ledger = ExplorationLedger.from_calls([
        _call("read_file",    "f1"),   # 1.0
        _call("search_code",  "q1"),   # 1.5
        _call("get_callers",  "s1"),   # 2.0
    ])
    assert ledger.diversity_score() == pytest.approx(4.5)


def test_duplicate_call_contributes_zero() -> None:
    ledger = ExplorationLedger.from_calls([
        _call("read_file", "f1"),
        _call("read_file", "f1"),   # exact duplicate → 0 credit
        _call("read_file", "f2"),   # new arg → full credit
    ])
    assert ledger.diversity_score() == pytest.approx(2.0)


def test_failed_call_still_contributes_to_score() -> None:
    ledger = ExplorationLedger.from_calls([
        _call("search_code", "q1", ok=False),  # failed grep still informative
    ])
    assert ledger.diversity_score() == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# ExplorationLedger.categories_covered
# ---------------------------------------------------------------------------


def test_categories_covered_excludes_failed_and_duplicates() -> None:
    ledger = ExplorationLedger.from_calls([
        _call("read_file",    "f1"),              # COMPREHENSION ✓
        _call("read_file",    "f1"),              # duplicate — still in COMPREHENSION
        _call("search_code",  "q1", ok=False),    # failed — no DISCOVERY credit
        _call("get_callers",  "s1"),              # CALL_GRAPH ✓
    ])
    covered = ledger.categories_covered()
    assert ExplorationCategory.COMPREHENSION in covered
    assert ExplorationCategory.CALL_GRAPH in covered
    assert ExplorationCategory.DISCOVERY not in covered
    assert len(covered) == 2


def test_unique_call_count_is_monotonic_signal() -> None:
    """Distinct successful (tool, args) pairs — the forward-progress metric."""
    ledger = ExplorationLedger.from_calls([
        _call("read_file",    "f1"),
        _call("read_file",    "f1"),             # duplicate
        _call("read_file",    "f2"),
        _call("get_callers",  "s1", ok=False),   # failed
        _call("get_callers",  "s1"),             # retry, now ok
    ])
    assert ledger.unique_call_count() == 3


# ---------------------------------------------------------------------------
# ExplorationLedger.from_records — duck-typing
# ---------------------------------------------------------------------------


def test_from_records_filters_non_exploration_tools() -> None:
    ledger = ExplorationLedger.from_records([
        FakeRecord("read_file",  "a"),
        FakeRecord("edit_file",  "b"),   # mutator — dropped
        FakeRecord("bash",       "c"),   # mutator — dropped
        FakeRecord("get_callers", "d"),
    ])
    assert len(ledger.calls) == 2
    assert ledger.diversity_score() == pytest.approx(3.0)


def test_from_records_respects_failure_status() -> None:
    ledger = ExplorationLedger.from_records([
        FakeRecord("read_file", "a", status="success"),
        FakeRecord("read_file", "b", status="error"),
    ])
    assert len(ledger.calls) == 2
    assert ledger.categories_covered() == frozenset({ExplorationCategory.COMPREHENSION})
    assert ledger.unique_call_count() == 1  # only the successful one


def test_from_records_tolerates_missing_attributes() -> None:
    class Minimal:
        tool_name = "read_file"
    ledger = ExplorationLedger.from_records([Minimal()])
    assert len(ledger.calls) == 1
    assert ledger.calls[0].arguments_hash == ""
    assert ledger.calls[0].succeeded is True


# ---------------------------------------------------------------------------
# ExplorationFloors.from_env
# ---------------------------------------------------------------------------


def test_floors_default_for_moderate(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "JARVIS_EXPLORATION_MIN_SCORE_MODERATE",
        "JARVIS_EXPLORATION_MIN_CATEGORIES_MODERATE",
    ):
        monkeypatch.delenv(var, raising=False)
    floors = ExplorationFloors.from_env("moderate")
    assert floors.min_score == 8.0
    assert floors.min_categories == 3
    assert floors.required_categories == frozenset()


def test_floors_trivial_is_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_EXPLORATION_MIN_SCORE_TRIVIAL", raising=False)
    monkeypatch.delenv("JARVIS_EXPLORATION_MIN_CATEGORIES_TRIVIAL", raising=False)
    floors = ExplorationFloors.from_env("trivial")
    assert floors.min_score == 0.0
    assert floors.min_categories == 0


def test_floors_architectural_requires_call_graph_and_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_EXPLORATION_MIN_SCORE_ARCHITECTURAL", raising=False)
    monkeypatch.delenv("JARVIS_EXPLORATION_MIN_CATEGORIES_ARCHITECTURAL", raising=False)
    floors = ExplorationFloors.from_env("architectural")
    assert ExplorationCategory.CALL_GRAPH in floors.required_categories
    assert ExplorationCategory.HISTORY    in floors.required_categories


def test_floors_env_overrides_score_and_categories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_EXPLORATION_MIN_SCORE_SIMPLE",      "12.5")
    monkeypatch.setenv("JARVIS_EXPLORATION_MIN_CATEGORIES_SIMPLE", "5")
    floors = ExplorationFloors.from_env("simple")
    assert floors.min_score == 12.5
    assert floors.min_categories == 5


def test_floors_unknown_complexity_falls_back_to_moderate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_EXPLORATION_MIN_SCORE_MODERATE", raising=False)
    floors = ExplorationFloors.from_env("galaxy-brain")
    assert floors.complexity == "moderate"
    assert floors.min_score == 8.0


def test_floors_malformed_env_value_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_EXPLORATION_MIN_SCORE_SIMPLE", "not-a-number")
    floors = ExplorationFloors.from_env("simple")
    assert floors.min_score == 4.0  # silent fallback, not a crash


# ---------------------------------------------------------------------------
# evaluate_exploration
# ---------------------------------------------------------------------------


def test_evaluate_sufficient_when_all_gates_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_EXPLORATION_MIN_SCORE_SIMPLE", raising=False)
    monkeypatch.delenv("JARVIS_EXPLORATION_MIN_CATEGORIES_SIMPLE", raising=False)
    ledger = ExplorationLedger.from_calls([
        _call("read_file",   "f1"),  # 1.0 COMPREHENSION
        _call("search_code", "q1"),  # 1.5 DISCOVERY
        _call("get_callers", "s1"),  # 2.0 CALL_GRAPH
    ])  # score 4.5, 3 categories
    floors = ExplorationFloors.from_env("simple")  # 4.0 / 2
    verdict = evaluate_exploration(ledger, floors)
    assert verdict.sufficient is True
    assert verdict.missing_categories == frozenset()
    assert verdict.score_deficit == 0.0


def test_evaluate_insufficient_when_score_below_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_EXPLORATION_MIN_SCORE_MODERATE", raising=False)
    monkeypatch.delenv("JARVIS_EXPLORATION_MIN_CATEGORIES_MODERATE", raising=False)
    ledger = ExplorationLedger.from_calls([
        _call("read_file", "f1"),  # 1.0 / 1 category
    ])
    verdict = evaluate_exploration(ledger, ExplorationFloors.from_env("moderate"))
    assert verdict.sufficient is False
    assert verdict.score_deficit == pytest.approx(7.0)  # 8.0 - 1.0
    assert verdict.category_deficit == 2                # need 3, have 1


def test_evaluate_insufficient_when_required_category_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_EXPLORATION_MIN_SCORE_ARCHITECTURAL", raising=False)
    monkeypatch.delenv("JARVIS_EXPLORATION_MIN_CATEGORIES_ARCHITECTURAL", raising=False)
    # High raw score but NO call_graph, NO history — architectural req not met
    ledger = ExplorationLedger.from_calls([
        _call("read_file",    "f1"),
        _call("read_file",    "f2"),
        _call("read_file",    "f3"),
        _call("read_file",    "f4"),
        _call("read_file",    "f5"),
        _call("search_code",  "q1"),
        _call("search_code",  "q2"),
        _call("search_code",  "q3"),
        _call("list_symbols", "s1"),
        _call("list_symbols", "s2"),
    ])  # score = 5*1.0 + 3*1.5 + 2*1.5 = 12.5 (still < 14 but covers the shape)
    verdict = evaluate_exploration(
        ledger, ExplorationFloors.from_env("architectural"),
    )
    assert verdict.sufficient is False
    assert ExplorationCategory.CALL_GRAPH in verdict.missing_categories
    assert ExplorationCategory.HISTORY    in verdict.missing_categories


# ---------------------------------------------------------------------------
# render_retry_feedback
# ---------------------------------------------------------------------------


def test_feedback_empty_when_sufficient() -> None:
    verdict = ExplorationVerdict(
        sufficient=True,
        score=5.0,
        score_deficit=0.0,
        categories_covered=frozenset({ExplorationCategory.COMPREHENSION}),
        missing_categories=frozenset(),
        category_deficit=0,
    )
    floors = ExplorationFloors(
        complexity="simple",
        min_score=4.0,
        min_categories=2,
    )
    assert render_retry_feedback(verdict, floors) == ""


def test_feedback_names_missing_required_categories() -> None:
    verdict = ExplorationVerdict(
        sufficient=False,
        score=10.0,
        score_deficit=4.0,
        categories_covered=frozenset({
            ExplorationCategory.COMPREHENSION,
            ExplorationCategory.DISCOVERY,
            ExplorationCategory.STRUCTURE,
        }),
        missing_categories=frozenset({
            ExplorationCategory.CALL_GRAPH,
            ExplorationCategory.HISTORY,
        }),
        category_deficit=1,
    )
    floors = ExplorationFloors(
        complexity="architectural",
        min_score=14.0,
        min_categories=4,
        required_categories=frozenset({
            ExplorationCategory.CALL_GRAPH,
            ExplorationCategory.HISTORY,
        }),
    )
    body = render_retry_feedback(verdict, floors)
    assert "EXPLORATION GATE" in body
    assert "call_graph" in body
    assert "history"    in body
    assert "architectural" in body


def test_feedback_mentions_score_widening_hint_when_deficit_positive() -> None:
    verdict = ExplorationVerdict(
        sufficient=False,
        score=1.0,
        score_deficit=7.0,
        categories_covered=frozenset({ExplorationCategory.COMPREHENSION}),
        missing_categories=frozenset(),
        category_deficit=2,
    )
    floors = ExplorationFloors(
        complexity="moderate",
        min_score=8.0,
        min_categories=3,
    )
    body = render_retry_feedback(verdict, floors)
    assert "Widen" in body or "widen" in body
    assert "get_callers" in body  # at least one concrete tool suggestion


# ---------------------------------------------------------------------------
# is_ledger_enabled
# ---------------------------------------------------------------------------


def test_ledger_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_EXPLORATION_LEDGER_ENABLED", raising=False)
    assert is_ledger_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_ledger_enabled_truthy_env_values(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    monkeypatch.setenv("JARVIS_EXPLORATION_LEDGER_ENABLED", val)
    assert is_ledger_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "maybe"])
def test_ledger_enabled_falsy_env_values(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    monkeypatch.setenv("JARVIS_EXPLORATION_LEDGER_ENABLED", val)
    assert is_ledger_enabled() is False


# ---------------------------------------------------------------------------
# Anti-gaming — failed calls can inflate score but must NOT inflate categories
# ---------------------------------------------------------------------------


def test_score_only_inflation_from_failed_calls_cannot_pass_category_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spamming failed-but-distinct calls must never satisfy the AND-gate.

    Failed calls accrue score (a failed grep is still signal) but are
    excluded from category coverage. If someone tries to game the score
    by emitting a flood of failing calls, the ``|covered| >= min_categories``
    conjunct keeps the gate closed.
    """
    monkeypatch.delenv("JARVIS_EXPLORATION_MIN_SCORE_MODERATE", raising=False)
    monkeypatch.delenv("JARVIS_EXPLORATION_MIN_CATEGORIES_MODERATE", raising=False)
    # 6 failed calls across 3 would-be categories → score 9.0 but 0 coverage.
    ledger = ExplorationLedger.from_calls([
        _call("read_file",    "f1", ok=False),
        _call("read_file",    "f2", ok=False),
        _call("search_code",  "q1", ok=False),
        _call("search_code",  "q2", ok=False),
        _call("get_callers",  "s1", ok=False),
        _call("get_callers",  "s2", ok=False),
    ])
    assert ledger.diversity_score() >= 8.0          # score gate would pass alone
    assert ledger.categories_covered() == frozenset()  # but no coverage
    verdict = evaluate_exploration(ledger, ExplorationFloors.from_env("moderate"))
    assert verdict.sufficient is False
    assert verdict.category_deficit == 3            # full category deficit


# ---------------------------------------------------------------------------
# Integration — ToolExecutionRecord field parity (prevents duck-type drift)
# ---------------------------------------------------------------------------


def test_tool_execution_record_field_parity() -> None:
    """Guard against silent duck-type drift.

    ``ExplorationLedger.from_records`` duck-types the real
    ``ToolExecutionRecord`` on four attributes: ``tool_name``,
    ``arguments_hash``, ``output_bytes``, ``status``. If any of those
    fields are renamed or removed on the orchestrator side, this test
    fires loudly so we don't lose exploration credit silently.
    """
    from backend.core.ouroboros.governance.tool_executor import (
        ToolExecStatus,
        ToolExecutionRecord,
    )

    record = ToolExecutionRecord(
        schema_version="tool.exec.v1",
        op_id="op-test",
        call_id="op-test:r0:read_file",
        round_index=0,
        tool_name="read_file",
        tool_version="1.0",
        arguments_hash="deadbeef",
        repo="jarvis",
        policy_decision="allow",
        policy_reason_code="",
        started_at_ns=1_000_000,
        ended_at_ns=2_000_000,
        duration_ms=1.0,
        output_bytes=128,
        error_class=None,
        status=ToolExecStatus.SUCCESS,
    )

    # All four duck-typed fields must resolve without AttributeError.
    assert hasattr(record, "tool_name")
    assert hasattr(record, "arguments_hash")
    assert hasattr(record, "output_bytes")
    assert hasattr(record, "status")

    # And the ledger must round-trip a single record into a single call.
    ledger = ExplorationLedger.from_records([record])
    assert len(ledger.calls) == 1
    call = ledger.calls[0]
    assert call.tool_name == "read_file"
    assert call.arguments_hash == "deadbeef"
    assert call.output_bytes == 128
    assert call.succeeded is True
    assert call.category is ExplorationCategory.COMPREHENSION
    assert ledger.diversity_score() == pytest.approx(1.0)
