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
    assert c.base_weight == 2.5


def test_call_unknown_tool_maps_to_uncategorized_and_zero_weight() -> None:
    c = _call("edit_file")  # mutator — must not be exploration
    assert c.category is ExplorationCategory.UNCATEGORIZED
    assert c.base_weight == 0.0


# ---------------------------------------------------------------------------
# ExplorationLedger.diversity_score
# ---------------------------------------------------------------------------


def test_score_sums_distinct_calls_by_base_weight() -> None:
    ledger = ExplorationLedger.from_calls([
        _call("read_file",    "f1"),   # 1.0 COMPREHENSION
        _call("search_code",  "q1"),   # 1.5 DISCOVERY
        _call("get_callers",  "s1"),   # 2.5 CALL_GRAPH
    ])
    # base = 5.0, 3 categories, multiplier = 1.0 + 0.5*(3-1) = 2.0
    # final = 5.0 * 2.0 = 10.0
    assert ledger.diversity_score() == pytest.approx(10.0)


def test_duplicate_call_contributes_zero() -> None:
    ledger = ExplorationLedger.from_calls([
        _call("read_file", "f1"),
        _call("read_file", "f1"),   # exact duplicate → 0 credit
        _call("read_file", "f2"),   # new arg → full credit
    ])
    assert ledger.diversity_score() == pytest.approx(2.0)


def test_failed_only_ledger_scores_zero_under_multiplier() -> None:
    """A ledger with only failed calls scores 0.0 under the diversity
    multiplier — failed calls accrue base weight but don't populate
    categories, and 0 categories means ``multiplier=0.0``.

    This strengthens the pre-multiplier anti-gaming property: previously,
    a failed call accrued raw base weight (the old assertion here was
    1.5 for one failed search_code). Under the multiplier, a failed-only
    ledger cannot inflate score at all, which is a more defensible
    semantic — "failed calls are signal but aren't exploration until
    at least one call succeeds in some category."
    """
    ledger = ExplorationLedger.from_calls([
        _call("search_code", "q1", ok=False),  # 1.5 base but 0 categories
    ])
    assert ledger.diversity_score() == pytest.approx(0.0)


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
        FakeRecord("read_file",  "a"),   # 1.0 COMPREHENSION
        FakeRecord("edit_file",  "b"),   # mutator — dropped
        FakeRecord("bash",       "c"),   # mutator — dropped
        FakeRecord("get_callers", "d"),  # 2.5 CALL_GRAPH
    ])
    assert len(ledger.calls) == 2
    # base = 3.5, 2 categories, multiplier = 1.5
    # final = 3.5 * 1.5 = 5.25
    assert ledger.diversity_score() == pytest.approx(5.25)


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
    assert floors.min_score == 3.5  # silent fallback, not a crash


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
    ])  # score = 5*1.0 + 3*1.5 + 2*1.5 = 12.5 (clears 11.0 score floor, but required CALL_GRAPH+HISTORY remain missing)
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
        min_score=11.0,
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


def test_feedback_sharpens_on_score_deficit_with_cats_satisfied() -> None:
    """Session E (bt-2026-04-15-063108) failure mode: model covered 3
    categories but picked low-leverage tools, scoring 9.0 / 10.0.

    When score is below floor, the sharpened feedback must fire
    unconditionally (not gated on cats-satisfied, per Session F fix)
    and must name:
      1. The HIGH-leverage tools (get_callers, git_blame)
      2. The MEDIUM-leverage fallback tools (search_code, list_symbols,
         git_log, git_diff)
      3. An explicit warning against padding with low-leverage tools
         (list_dir, glob_files)

    This test covers the "cats satisfied, score deficit" case — the
    first of two unconditional-fire scenarios. The second case (cats
    NOT satisfied, score deficit) is covered by
    ``test_feedback_sharpens_even_when_categories_not_yet_satisfied``
    below, which is the direct regression for Session F.
    """
    verdict = ExplorationVerdict(
        sufficient=False,
        score=9.0,
        score_deficit=1.0,
        categories_covered=frozenset({
            ExplorationCategory.COMPREHENSION,
            ExplorationCategory.DISCOVERY,
            ExplorationCategory.STRUCTURE,
        }),  # 3 categories — matches min_categories exactly
        missing_categories=frozenset(),
        category_deficit=0,
    )
    floors = ExplorationFloors(
        complexity="complex",
        min_score=10.0,
        min_categories=3,
    )
    body = render_retry_feedback(verdict, floors)

    # The sharpened score-gate block must fire
    assert "SCORE GATE" in body
    assert "deficit 1.0" in body

    # High-leverage tools explicitly named
    assert "get_callers" in body
    assert "git_blame" in body

    # Medium-leverage tools as fallback
    assert "search_code" in body
    assert "list_symbols" in body
    assert "git_log" in body

    # Explicit warning against low-leverage padding
    assert "LOW-LEVERAGE" in body or "low-leverage" in body.lower()
    assert "list_dir" in body
    assert "glob_files" in body
    assert "DO NOT" in body  # the explicit don't-pad directive


def test_feedback_sharpens_even_when_categories_not_yet_satisfied() -> None:
    """Session F (bt-2026-04-15-065523) direct regression test.

    Failure mode: attempt 1 had ``categories=2/3`` (comprehension +
    discovery) and ``score=4.5/10.0``. The pre-Session-F sharpened
    branch was gated on ``categories_satisfied``, so it DID NOT fire
    for this state — the model received only the soft "Widen your
    exploration" legacy hint and responded by adding another
    low-leverage tool (list_symbols) plus another list_dir, still
    falling short of the score floor and dying on the retry synthesis.

    Post-fix: the sharpened high-leverage block fires
    UNCONDITIONALLY on any score deficit, so this verdict state
    (Session F attempt 1 exactly) now receives the full warning
    including the explicit ``get_callers`` / ``git_blame`` /
    ``search_code`` tool names and the "DO NOT pad with list_dir"
    directive.

    If this test ever regresses, it means the 2-attempt retry loop
    has again become a dead zone for sharpened feedback — the bug
    Session F existed to diagnose.
    """
    verdict = ExplorationVerdict(
        sufficient=False,
        score=4.5,
        score_deficit=5.5,
        categories_covered=frozenset({
            ExplorationCategory.COMPREHENSION,
            ExplorationCategory.DISCOVERY,
        }),  # 2 categories — NOT satisfied against min_categories=3
        missing_categories=frozenset(),
        category_deficit=1,
    )
    floors = ExplorationFloors(
        complexity="complex",
        min_score=10.0,
        min_categories=3,
    )
    body = render_retry_feedback(verdict, floors)

    # The sharpened score-gate block MUST fire even though cats are
    # not yet at the floor. This is the Session F regression guard.
    assert "SCORE GATE" in body
    assert "deficit 5.5" in body

    # All three leverage tiers explicitly named
    assert "get_callers" in body
    assert "git_blame" in body
    assert "search_code" in body
    assert "list_symbols" in body

    # Warning against low-leverage padding — the specific behavior that
    # tanked Session F's retry
    assert "LOW-LEVERAGE" in body or "low-leverage" in body.lower()
    assert "list_dir" in body
    assert "glob_files" in body
    assert "DO NOT" in body

    # And — critically — the category-gate guidance is ALSO present
    # (category_deficit=1 means one category is still missing). Both
    # messages must coexist in the right order: cats-guidance above,
    # score-gate guidance below.
    _cat_gate_marker = "Categories covered: 2"
    _score_gate_marker = "SCORE GATE"
    assert _cat_gate_marker in body
    assert _score_gate_marker in body
    assert body.index(_cat_gate_marker) < body.index(_score_gate_marker), (
        "Cats-guidance must render ABOVE score-gate guidance so the model "
        "sees 'fill gaps' before 'use high-leverage tools to fill them'"
    )


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

    Failed calls are excluded from category coverage. Under the old
    linear formula this test asserted that ``diversity_score() >= 8.0``
    (i.e. failed calls still inflated the raw base sum) and relied
    purely on the ``|covered| >= min_categories`` conjunct to close
    the gate. Under the new diversity-multiplier formula, a ledger with
    zero categories has ``multiplier = 0.0``, so the score itself also
    collapses to 0.0 — a STRONGER anti-gaming property: the adversary
    can no longer even inflate score, let alone pass the category gate.
    """
    monkeypatch.delenv("JARVIS_EXPLORATION_MIN_SCORE_MODERATE", raising=False)
    monkeypatch.delenv("JARVIS_EXPLORATION_MIN_CATEGORIES_MODERATE", raising=False)
    # 6 failed calls across 3 would-be categories → base 9.0 but 0 coverage
    # → multiplier 0.0 → final score 0.0.
    ledger = ExplorationLedger.from_calls([
        _call("read_file",    "f1", ok=False),
        _call("read_file",    "f2", ok=False),
        _call("search_code",  "q1", ok=False),
        _call("search_code",  "q2", ok=False),
        _call("get_callers",  "s1", ok=False),
        _call("get_callers",  "s2", ok=False),
    ])
    assert ledger.diversity_score() == pytest.approx(0.0)  # multiplier zeroes it
    assert ledger.categories_covered() == frozenset()      # no coverage either
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
    # 1 call * 1.0 base, 1 category → multiplier 1.0 → score 1.0
    assert ledger.diversity_score() == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 2026-04-14 calibration — diversity multiplier, category remap, complex floor
# ---------------------------------------------------------------------------


def test_list_dir_is_discovery_category_not_comprehension() -> None:
    """Remap landed 2026-04-14: ``list_dir`` answers "what exists here?",
    which is a discovery question, not a comprehension one. Pre-remap it
    was bucketed with ``read_file``, which punished models that
    diversified by adding ``list_dir`` to a read-heavy exploration.
    """
    call = _call("list_dir", "/")
    assert call.category is ExplorationCategory.DISCOVERY
    assert call.category is not ExplorationCategory.COMPREHENSION


def test_floors_default_for_complex_is_dedicated_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session bt-2026-04-15-044627 exposed a silent fallback: ``complex``
    complexity wasn't in ``_DEFAULT_FLOORS`` so it was enforced against
    ``moderate`` defaults. New dedicated entry lands at 10.0/3 — JUST
    BELOW the minimum reasonable 4-file exploration pattern (read_file×2
    + search_code + list_symbols = 5.0 base × 2.0 mult = 10.0).
    """
    for var in (
        "JARVIS_EXPLORATION_MIN_SCORE_COMPLEX",
        "JARVIS_EXPLORATION_MIN_CATEGORIES_COMPLEX",
    ):
        monkeypatch.delenv(var, raising=False)
    floors = ExplorationFloors.from_env("complex")
    assert floors.complexity == "complex"
    assert floors.min_score == 10.0
    assert floors.min_categories == 3
    assert floors.required_categories == frozenset()


def test_base_score_is_hard_capped_against_spam() -> None:
    """An adversarial "read every file in the repo" strategy must not
    out-score diverse exploration through sheer volume. 50 unique
    read_file calls accrue base 50.0 but are clipped to
    ``_BASE_SCORE_CAP`` (15.0) and the 1-category multiplier (1.0) keeps
    the final score at the cap — still well under the complex floor
    (10.0) only because the category gate (3 required) closes it, but
    the spam ceiling itself bounds raw score at 15.0.
    """
    calls = [_call("read_file", f"f{i}") for i in range(50)]
    ledger = ExplorationLedger.from_calls(calls)
    # 50 unique reads → 50.0 base, clipped to 15.0. 1 category → ×1.0.
    assert ledger.diversity_score() == pytest.approx(15.0)
    assert len(ledger.categories_covered()) == 1


@pytest.mark.parametrize(
    "label,calls,expected_score,expected_cats,passes_complex",
    [
        # Sessions A/B retry: 4× read_file — shallow spam, 1 category
        (
            "session_ab_shallow",
            [_call("read_file", f"f{i}") for i in range(4)],
            4.0,    # 4.0 base × 1.0 mult
            1,
            False,  # fails complex on both score (4<10) and cats (1<3)
        ),
        # Session D retry post-remap: 2× read_file + list_dir
        # (list_dir moved from COMPREHENSION to DISCOVERY this PR)
        (
            "session_d_retry_post_remap",
            [
                _call("read_file", "f1"),
                _call("read_file", "f2"),
                _call("list_dir",  "/"),
            ],
            3.75,   # (1.0+1.0+0.5) * 1.5(2cat) = 3.75
            2,
            False,  # fails complex cats (2<3); diverse but still thin
        ),
        # P3 — minimum reasonable 4-file exploration: clears complex floor
        (
            "p3_minimum_reasonable",
            [
                _call("read_file",    "f1"),
                _call("read_file",    "f2"),
                _call("search_code",  "q1"),
                _call("list_symbols", "s1"),
            ],
            10.0,   # (1+1+1.5+1.5) * 2.0(3cat) = 10.0
            3,
            True,   # exactly at the floor — passes
        ),
        # P4 — good exploration: well above complex floor
        (
            "p4_good_exploration",
            [
                _call("read_file",    "f1"),
                _call("read_file",    "f2"),
                _call("read_file",    "f3"),
                _call("read_file",    "f4"),
                _call("search_code",  "q1"),
                _call("search_code",  "q2"),
                _call("list_symbols", "s1"),
                _call("list_symbols", "s2"),
            ],
            20.0,   # (4+3+3) * 2.0(3cat) = 20.0
            3,
            True,
        ),
        # P6 — adversarial spam: cap + no category breadth = no win
        (
            "p6_adversarial_spam",
            [_call("read_file", f"f{i}") for i in range(50)],
            15.0,   # 50.0 base capped to 15.0, 1 cat → ×1.0 = 15.0
            1,
            False,  # high score but fails cats (1<3)
        ),
        # Sanity: read_file + search_code (user's simple-tier check)
        (
            "simple_tier_sanity_read_plus_search",
            [
                _call("read_file",   "f1"),
                _call("search_code", "q1"),
            ],
            3.75,   # (1.0+1.5) * 1.5(2cat) = 3.75, passes simple (3.5/2)
            2,
            False,  # simple tier passes but not complex
        ),
    ],
)
def test_diversity_multiplier_worked_examples(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    calls: list,
    expected_score: float,
    expected_cats: int,
    passes_complex: bool,
) -> None:
    """Lock the scoring math against the worked examples from the PR
    design table. Any change to tool weights, category mapping, cap,
    or multiplier shape must update these numbers deliberately —
    breakage here is by design the load-bearing test for calibration
    regression.
    """
    for var in (
        "JARVIS_EXPLORATION_MIN_SCORE_COMPLEX",
        "JARVIS_EXPLORATION_MIN_CATEGORIES_COMPLEX",
    ):
        monkeypatch.delenv(var, raising=False)
    ledger = ExplorationLedger.from_calls(calls)
    assert ledger.diversity_score() == pytest.approx(expected_score), label
    assert len(ledger.categories_covered()) == expected_cats, label
    verdict = evaluate_exploration(
        ledger, ExplorationFloors.from_env("complex"),
    )
    assert verdict.sufficient is passes_complex, label


def test_simple_tier_minimum_pattern_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check: the minimum simple-op exploration (read target + do
    one breadth action) MUST pass the ``simple`` floor under the new
    math. ``read_file + search_code`` = 2.5 base × 1.5 mult = 3.75,
    against a floor of 3.5. Raising the floor to 4.0 would silently
    break this case; dropping to 3.5 accepts it cleanly.
    """
    for var in (
        "JARVIS_EXPLORATION_MIN_SCORE_SIMPLE",
        "JARVIS_EXPLORATION_MIN_CATEGORIES_SIMPLE",
    ):
        monkeypatch.delenv(var, raising=False)
    ledger = ExplorationLedger.from_calls([
        _call("read_file",   "f1"),
        _call("search_code", "q1"),
    ])
    floors = ExplorationFloors.from_env("simple")
    assert floors.min_score == 3.5
    verdict = evaluate_exploration(ledger, floors)
    assert verdict.sufficient is True
    assert verdict.score == pytest.approx(3.75)
