"""n>1 candidates per op, and a per-candidate verdict to tell them apart.

A DPO preference pair needs two answers to ONE question. The local lane
produced exactly one candidate per op, so the trajectory corpus could not
yield a pair however long a farming soak ran.

Generating siblings is necessary but NOT sufficient, and that is the part
worth pinning: the recorder stamped the op's single terminal verdict onto
every candidate of that op, so three siblings scored identically in the DPO
ranker and were discarded by its equal-outcome guard. Measured against the
real reactor generator: 3 uniform siblings -> 0 pairs; the same 3 with
per-candidate outcomes -> 2 pairs (method=outcome_diff, delta 0.75).

So the second half of this slice routes the verdict VALIDATE already
computes per candidate -- it was going to the ledger and nowhere else --
into the recorder, where it overrides the op-level outcome for that one
sibling.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance.candidate_generator import (
    _sibling_budget_margin,
    _sibling_candidate_count,
)

_ENV_N = "JARVIS_LOCAL_SIBLING_CANDIDATES"
_ENV_MARGIN = "JARVIS_LOCAL_SIBLING_BUDGET_MARGIN"


# --------------------------------------------------------------------------
# The knobs
# --------------------------------------------------------------------------


def test_default_is_three_because_two_is_the_minimum_that_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_ENV_N, raising=False)
    assert _sibling_candidate_count() == 3


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("1", 1),      # explicit opt-out -> legacy single candidate
        ("2", 2),      # the minimum that can pair
        ("8", 8),
        ("300", 8),    # clamped: siblings are sequential on ONE gpu
        ("0", 1),
        ("-5", 1),
        ("banana", 3),  # unparseable -> default, never a crash
        ("", 3),
    ],
)
def test_count_is_clamped(
    monkeypatch: pytest.MonkeyPatch, raw: str, expect: int,
) -> None:
    """The upper clamp is load-bearing, not decoration.

    Siblings are sequential, so a fat-fingered 300 would spend the whole op
    budget generating and leave nothing for VALIDATE -- which is where the
    per-candidate verdict that makes siblings worth having comes from.
    """
    monkeypatch.setenv(_ENV_N, raw)
    assert _sibling_candidate_count() == expect


def test_margin_floors_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Below 1.0 the check would start siblings the budget cannot finish."""
    monkeypatch.setenv(_ENV_MARGIN, "0.1")
    assert _sibling_budget_margin() == 1.0
    monkeypatch.setenv(_ENV_MARGIN, "2.5")
    assert _sibling_budget_margin() == 2.5


# --------------------------------------------------------------------------
# The sibling loop
# --------------------------------------------------------------------------


#: Genuinely different ANSWERS -- different control flow, not different
#: names. The fixtures used to be `f"# {h}\n"`, a bare comment: comments
#: never reach the AST, so every one of them was the same empty module.
#: That was invisible while dedup was byte-wise and became load-bearing
#: the moment acceptance turned structural, which is the whole point of
#: the change -- "different bytes" and "different logic" are not the same
#: predicate, and the corpus was full of pairs that satisfied only the
#: first.
_SHAPES: Tuple[str, ...] = (
    "def run(x):\n    return x + 1\n",
    "def run(x):\n    total = 0\n    for i in range(x):\n"
    "        total += i\n    return total\n",
    "def run(x):\n    if x > 0:\n        return x\n"
    "    while x < 0:\n        x += 1\n    return x\n",
    "class R:\n    def go(self, x):\n        try:\n"
    "            return x / 2\n        except ZeroDivisionError:\n"
    "            return 0\n",
)


def _cand(h: str) -> Dict[str, Any]:
    """One candidate whose SHAPE is a deterministic function of ``h``.

    Same ``h`` -> same logic (a real duplicate); different ``h`` -> a
    different control-flow shape, so a merge test is exercising the case
    it claims to.
    """
    return {
        "candidate_id": h, "candidate_hash": h, "file_path": "m.py",
        "full_content": _SHAPES[sum(ord(c) for c in h) % len(_SHAPES)],
    }


class _Result:
    """Minimal GenerationResult stand-in (dataclasses.replace-compatible)."""

    def __init__(self, cands: List[Dict[str, Any]], duration: float = 1.0):
        self.candidates = tuple(cands)
        self.generation_duration_s = duration


# `dataclasses.replace` needs a real dataclass; use the real one.
def _real_result(cands: List[Dict[str, Any]], duration: float = 1.0) -> Any:
    from backend.core.ouroboros.governance.op_context import GenerationResult

    return GenerationResult(
        candidates=tuple(cands),
        provider_name="local",
        generation_duration_s=duration,
    )


class _Gen:
    """Just enough CandidateGenerator to drive _extend_with_siblings."""

    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator as _CG,
    )

    _extend_with_siblings = _CG._extend_with_siblings
    _profiler_for_siblings = _CG._profiler_for_siblings
    # Already a staticmethod on CandidateGenerator; the class-attribute
    # access hands back the plain function, so re-wrap it.
    _remaining_seconds = staticmethod(_CG._remaining_seconds)


def _run(first: Any, attempts: List[Any], *, budget_s: float = 600.0,
         prefill: str = "", sampling_log: Optional[List[Any]] = None) -> Any:
    """Drive the loop with a scripted sequence of sibling attempts.

    ``_attempt`` takes the DRAW's sampling point, mirroring the real
    ``_attempts`` closure. ``sampling_log`` collects what each draw was
    handed, so a test can assert the entropy ladder actually reached the
    request rather than merely being computed.
    """
    calls = {"n": 0}

    async def _attempt(sampling: Any = None) -> Any:
        if sampling_log is not None:
            sampling_log.append(sampling)
        i = calls["n"]
        calls["n"] += 1
        item = attempts[i] if i < len(attempts) else None
        if isinstance(item, BaseException):
            raise item
        return item

    ctx = type("_C", (), {"op_id": "op-123"})()
    deadline = datetime.now(timezone.utc) + timedelta(seconds=budget_s)
    out = asyncio.run(
        _Gen()._extend_with_siblings(
            first, _attempt, ctx, deadline, resume_prefill=prefill,
        )
    )
    return out, calls["n"]


def test_siblings_are_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV_N, "3")
    first = _real_result([_cand("a")])
    out, n_calls = _run(
        first, [_real_result([_cand("b")]), _real_result([_cand("c")])],
    )
    assert [c["candidate_hash"] for c in out.candidates] == ["a", "b", "c"]
    assert n_calls == 2  # first candidate came from the caller, not the loop


def test_identical_siblings_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A duplicate is pure cost: the pair generator discards it anyway."""
    monkeypatch.setenv(_ENV_N, "3")
    first = _real_result([_cand("a")])
    out, _ = _run(first, [_real_result([_cand("a")]), _real_result([_cand("b")])])
    assert [c["candidate_hash"] for c in out.candidates] == ["a", "b"]


def test_n_equals_one_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """The opt-out must not even attempt a sibling."""
    monkeypatch.setenv(_ENV_N, "1")
    first = _real_result([_cand("a")])
    out, n_calls = _run(first, [_real_result([_cand("b")])])
    assert out is first
    assert n_calls == 0


def test_a_failing_sibling_never_costs_the_op_its_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Siblings are a bonus. The FIRST attempt propagates; these swallow.

    If a sibling exception escaped, adding n>1 would convert ops that
    succeed today into failures -- the single worst way this could go
    wrong, since it trades working repairs for training data.
    """
    monkeypatch.setenv(_ENV_N, "4")
    first = _real_result([_cand("a")])
    out, _ = _run(first, [RuntimeError("engine died"), _real_result([_cand("z")])])
    assert [c["candidate_hash"] for c in out.candidates] == ["a"]


def test_empty_sibling_stops_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV_N, "4")
    first = _real_result([_cand("a")])
    out, n_calls = _run(first, [None, _real_result([_cand("b")])])
    assert [c["candidate_hash"] for c in out.candidates] == ["a"]
    assert n_calls == 1


def test_tight_budget_degrades_to_one_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The op deadline is never extended; a sibling is drawn from slack.

    The first candidate cost 40s, the margin is 1.5, so a sibling needs 60s
    of slack and only 10s remain. Today's behaviour, silently.
    """
    monkeypatch.setenv(_ENV_N, "3")
    monkeypatch.setenv(_ENV_MARGIN, "1.5")
    first = _real_result([_cand("a")], duration=40.0)
    out, n_calls = _run(first, [_real_result([_cand("b")])], budget_s=10.0)
    assert out is first
    assert n_calls == 0


def test_resume_never_draws_siblings(monkeypatch: pytest.MonkeyPatch) -> None:
    """A RESUME continues ONE interrupted thought.

    Drawing alternatives to it would answer a different question, and the
    prefill would be prepended to each -- producing siblings that share a
    head and diverge only in the tail.
    """
    monkeypatch.setenv(_ENV_N, "3")
    first = _real_result([_cand("a")])
    out, n_calls = _run(
        first, [_real_result([_cand("b")])], prefill="def half_written(",
    )
    assert out is first
    assert n_calls == 0


def test_no_candidates_returns_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV_N, "3")
    out, n_calls = _run(None, [_real_result([_cand("b")])])
    assert out is None
    assert n_calls == 0


# --------------------------------------------------------------------------
# Per-candidate verdicts: the half that makes siblings mean something
# --------------------------------------------------------------------------


@pytest.fixture()
def _recorder(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from backend.core.ouroboros.governance.observability import (
        trajectory_recorder as tr,
    )

    monkeypatch.setenv("JARVIS_TRAJECTORY_RECORDER_ENABLED", "true")
    # Pass the path EXPLICITLY rather than steering it through the
    # directory env var. Naming that variable wrong does not fail -- it
    # silently redirects the write to the operator's real corpus at
    # ~/.jarvis/trinity/events/, where synthetic rows are indistinguishable
    # from farmed ones and would be trained on. The override argument is
    # the only form that cannot miss.
    tr.reset_recorder_for_tests(path=tmp_path / "events" / "trajectories.jsonl")
    yield tr
    tr.reset_recorder_for_tests()


def _pending(tr, op_id: str, hashes: List[str]):
    return tr._PendingGeneration(
        op_id=op_id, prompt="fix it", prompt_key="k",
        candidates=tuple(_cand(h) for h in hashes),
        model_id="qwen3-coder:30b", provider_name="local", is_noop=False,
        latency_ms=1000.0, prompt_tokens=30, completion_tokens=98,
        cost_usd=0.0, task_type="code_repair", session_id="s",
        tokens_estimated=False,
    )


def _rows_for(rec, op_id: str) -> List[Dict[str, Any]]:
    """Rows for ONE op.

    The corpus is a shared append-only JSONL in production too, so a read
    that assumes it holds only this op is testing something untrue.
    """
    import json

    return [
        r for r in (
            json.loads(ln)
            for ln in rec.path.read_text(encoding="utf-8").splitlines() if ln
        )
        if r["metadata"]["op_id"] == op_id
    ]


def test_failed_sibling_overrides_a_successful_op(_recorder) -> None:
    """The whole point: siblings of one op end with DIFFERENT outcomes.

    A candidate VALIDATE rejected was bad on its own merits, regardless of
    how the op later ended. Without this override all three rows carry the
    op's "success", score identically, and the ranker emits nothing.
    """
    tr = _recorder
    OP = "op-1"

    async def _go() -> List[Dict[str, Any]]:
        import json

        rec = tr.get_recorder()
        gen = _pending(tr, "op-1", ["good", "bad1", "bad2"])
        await rec._admit_pending(gen)
        for h, ok in (("good", True), ("bad1", False), ("bad2", False)):
            await rec._attach_verdict(
                tr._CandidateVerdictEvent(
                    op_id="op-1", candidate_hash=h, passed=ok,
                    failure_class="" if ok else "tests_failed",
                )
            )
        await rec._resolve(
            tr._OutcomeEvent(
                op_id="op-1", terminal_phase="COMPLETED",
                terminal_reason="applied",
            )
        )
        return _rows_for(rec, OP)

    rows = asyncio.run(_go())
    by_hash = {r["metadata"]["candidate_hash"]: r for r in rows}
    assert len(rows) == 3

    assert by_hash["good"]["outcome"] == "success"
    assert by_hash["good"]["confidence"] == 1.0
    assert by_hash["bad1"]["outcome"] == "failure"
    assert by_hash["bad2"]["outcome"] == "failure"
    assert by_hash["bad1"]["confidence"] == 0.0

    # ...and the corpus says WHERE each verdict came from, so a sibling set
    # that yields no pair explains itself without re-deriving why.
    assert by_hash["good"]["metadata"]["verdict_source"] == "candidate"
    assert by_hash["bad1"]["metadata"]["candidate_validated"] is False
    assert by_hash["good"]["metadata"]["candidate_validated"] is True

    # A candidate the pipeline rejected is model quality, so it trains.
    assert by_hash["bad1"]["metadata"]["should_train"] is True
    assert by_hash["bad1"]["metadata"]["terminal_reason"] == "tests_failed"


def test_without_verdicts_rows_are_marked_inherited(_recorder) -> None:
    """The pre-slice shape, explicitly labelled rather than indistinguishable."""
    tr = _recorder
    OP = "op-2"

    async def _go() -> List[Dict[str, Any]]:
        import json

        rec = tr.get_recorder()
        await rec._admit_pending(_pending(tr, "op-2", ["a", "b"]))
        await rec._resolve(
            tr._OutcomeEvent(
                op_id="op-2", terminal_phase="COMPLETED",
                terminal_reason="applied",
            )
        )
        return _rows_for(rec, OP)

    rows = asyncio.run(_go())
    assert {r["outcome"] for r in rows} == {"success"}
    assert {r["metadata"]["verdict_source"] for r in rows} == {"operation"}
    assert all(r["metadata"]["candidate_validated"] is None for r in rows)


def test_passing_sibling_inherits_a_caged_op(_recorder) -> None:
    """A pass earns only "not broken"; what happened next is the op's verdict.

    A governance refusal is not model quality, so the passing candidate
    stays non-trainable -- while the sibling VALIDATE actually rejected is
    still trainable on its own evidence.
    """
    tr = _recorder
    OP = "op-3"

    async def _go() -> Dict[str, Any]:
        import json

        rec = tr.get_recorder()
        await rec._admit_pending(_pending(tr, "op-3", ["ok", "broken"]))
        for h, passed in (("ok", True), ("broken", False)):
            await rec._attach_verdict(
                tr._CandidateVerdictEvent(
                    op_id="op-3", candidate_hash=h, passed=passed,
                    failure_class="" if passed else "syntax",
                )
            )
        await rec._resolve(
            tr._OutcomeEvent(
                op_id="op-3", terminal_phase="BLOCKED",
                terminal_reason="touches_kernel",
            )
        )
        return {r["metadata"]["candidate_hash"]: r for r in _rows_for(rec, OP)}

    by_hash = asyncio.run(_go())
    assert by_hash["ok"]["outcome"] == "unknown"
    assert by_hash["ok"]["metadata"]["should_train"] is False
    assert by_hash["broken"]["outcome"] == "failure"
    assert by_hash["broken"]["metadata"]["should_train"] is True


def test_verdict_without_a_hash_is_dropped_not_guessed(_recorder) -> None:
    """An unjoinable verdict must never be attached to an arbitrary sibling."""
    tr = _recorder
    rec = tr.get_recorder()
    assert rec.record_candidate_verdict(
        op_id="op-4", candidate_hash="", passed=False,
    ) is False


def test_orphan_verdict_is_counted_not_raised(_recorder) -> None:
    """Telemetry never breaks the loop it observes."""
    tr = _recorder

    async def _go() -> Dict[str, int]:
        rec = tr.get_recorder()
        await rec._attach_verdict(
            tr._CandidateVerdictEvent(
                op_id="nope", candidate_hash="h", passed=True, failure_class="",
            )
        )
        return dict(rec.stats())

    stats = asyncio.run(_go())
    assert stats["orphan_candidate_verdicts"] == 1
    assert stats["candidate_verdicts_joined"] == 0


# --------------------------------------------------------------------------
# Structural diversity: a sibling must add LOGIC, not just bytes
# --------------------------------------------------------------------------


def _shaped(h: str, shape: int) -> Dict[str, Any]:
    """A candidate with an explicit shape, so a test can force a duplicate.

    ``_cand`` derives shape from the hash; these tests need the two
    decoupled -- a different hash carrying the SAME logic is exactly the
    pair that byte-wise dedup shipped and structural dedup must reject.
    """
    return {
        "candidate_id": h, "candidate_hash": h, "file_path": "m.py",
        "full_content": _SHAPES[shape % len(_SHAPES)],
    }


def test_a_redundant_sibling_is_redrawn_at_higher_entropy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The corpus failure, reproduced and fixed.

    Draw 2 comes back with a DIFFERENT hash and the SAME logic -- the
    shape that filled the trajectory corpus with rows that could never
    become a preference pair. It must be rejected and re-drawn, and the
    re-draw must be handed a hotter sampling point than the draw that
    failed.
    """
    monkeypatch.setenv(_ENV_N, "2")
    monkeypatch.setenv("JARVIS_SIBLING_MAX_RESAMPLE", "1")
    log: List[Any] = []
    first = _real_result([_shaped("a", 1)])
    out, n_calls = _run(
        first,
        [
            _real_result([_shaped("b", 1)]),   # same logic, different hash
            _real_result([_shaped("c", 2)]),   # genuinely different
        ],
        sampling_log=log,
    )
    assert n_calls == 2, "the redundant draw was not re-taken"
    assert [c["candidate_hash"] for c in out.candidates] == ["a", "c"]
    assert len(log) == 2
    assert log[1].temperature > log[0].temperature
    assert log[0].seed != log[1].seed


def test_a_sibling_that_stays_redundant_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persisting it would write a row that LOOKS like half of a pair."""
    monkeypatch.setenv(_ENV_N, "2")
    monkeypatch.setenv("JARVIS_SIBLING_MAX_RESAMPLE", "1")
    first = _real_result([_shaped("a", 1)])
    out, n_calls = _run(
        first,
        [_real_result([_shaped("b", 1)]), _real_result([_shaped("c", 1)])],
    )
    assert n_calls == 2
    assert out is first, "a redundant sibling must not reach the corpus"


def test_a_structurally_new_sibling_is_taken_first_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No re-draw when the draw already added logic -- re-draws cost budget."""
    monkeypatch.setenv(_ENV_N, "2")
    log: List[Any] = []
    first = _real_result([_shaped("a", 1)])
    out, n_calls = _run(
        first, [_real_result([_shaped("b", 2)])], sampling_log=log,
    )
    assert n_calls == 1
    assert [c["candidate_hash"] for c in out.candidates] == ["a", "b"]


def test_every_sibling_draw_leaves_the_legacy_sampling_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Draw 1 is the op's own candidate; draws 2+ are the ones that explore.

    Every sibling that reached the model at temperature 0.2 was a draw
    from a near-deterministic distribution -- which is why the corpus
    filled with near-identical answers.
    """
    monkeypatch.setenv(_ENV_N, "3")
    log: List[Any] = []
    first = _real_result([_shaped("a", 0)])
    _run(
        first,
        [_real_result([_shaped("b", 1)]), _real_result([_shaped("c", 2)])],
        sampling_log=log,
    )
    assert len(log) == 2
    assert all(s is not None and not s.is_legacy for s in log)
    assert [s.temperature for s in log] == sorted(s.temperature for s in log)


def test_master_switch_off_restores_byte_wise_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entropy OFF: legacy sampling, and a same-logic sibling is kept again."""
    monkeypatch.setenv(_ENV_N, "2")
    monkeypatch.setenv("JARVIS_SIBLING_ENTROPY_ENABLED", "false")
    log: List[Any] = []
    first = _real_result([_shaped("a", 1)])
    out, n_calls = _run(
        first, [_real_result([_shaped("b", 1)])], sampling_log=log,
    )
    assert n_calls == 1
    assert all(s.is_legacy for s in log)
    assert [c["candidate_hash"] for c in out.candidates] == ["a", "b"]


# --------------------------------------------------------------------------
# An answer is not a dead lane (soak bt-2026-09-03-072129: 15 singletons)
# --------------------------------------------------------------------------


def _noop_result(duration: float = 1.0) -> Any:
    """What the provider hands back for ``2b.1-noop``: no candidates,
    ``is_noop=True``. The recorder never queues it."""
    from backend.core.ouroboros.governance.op_context import GenerationResult

    return GenerationResult(
        candidates=(), provider_name="local",
        generation_duration_s=duration, is_noop=True,
    )


def _syntax_exc() -> RuntimeError:
    """The provider's real contract for unparseable Python: a RuntimeError
    carrying ``syntax_failures`` -- the attribute the primary path's repair
    already keys on."""
    exc = RuntimeError("local_schema_invalid:all_candidates_syntax_error")
    exc.syntax_failures = [{"file_path": "m.py", "line": 3,   # type: ignore[attr-defined]
                            "message": "unexpected indent"}]
    return exc


def test_a_noop_sibling_is_redrawn_not_a_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """8 of soak 19's 15 singletons: the sibling said "already done".

    That is the model answering the prompt, not the engine dying. It must
    be re-drawn at a hotter point, exactly like a redundant draw.
    """
    monkeypatch.setenv(_ENV_N, "2")
    monkeypatch.setenv("JARVIS_SIBLING_MAX_RESAMPLE", "1")
    log: List[Any] = []
    first = _real_result([_shaped("a", 1)])
    out, n_calls = _run(
        first, [_noop_result(), _real_result([_shaped("b", 2)])],
        sampling_log=log,
    )
    assert n_calls == 2, "the no-op was treated as a stop"
    assert [c["candidate_hash"] for c in out.candidates] == ["a", "b"]
    assert log[1].temperature > log[0].temperature
    assert log[0].seed != log[1].seed


def test_a_noop_at_the_cap_drops_the_slot_and_tries_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The slot is dropped, the LOOP is not: slot 3 still gets its draw."""
    monkeypatch.setenv(_ENV_N, "3")
    monkeypatch.setenv("JARVIS_SIBLING_MAX_RESAMPLE", "1")
    first = _real_result([_shaped("a", 1)])
    out, n_calls = _run(
        first,
        [_noop_result(), _noop_result(),            # slot 2: noop, noop -> dropped
         _real_result([_shaped("c", 2)])],          # slot 3: merged
    )
    assert n_calls == 3
    assert [c["candidate_hash"] for c in out.candidates] == ["a", "c"]


def test_a_none_result_still_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lane, not the answer: an engine that returns nothing is still a
    stop, byte-for-byte the pinned behaviour above."""
    monkeypatch.setenv(_ENV_N, "4")
    first = _real_result([_cand("a")])
    out, n_calls = _run(first, [None, _real_result([_cand("b")])])
    assert [c["candidate_hash"] for c in out.candidates] == ["a"]
    assert n_calls == 1


def test_an_unparseable_sibling_is_redrawn_not_a_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``all_candidates_syntax_error`` is a bad answer. Its carrier is the
    provider's own ``syntax_failures`` attribute -- no type sniffing."""
    monkeypatch.setenv(_ENV_N, "2")
    monkeypatch.setenv("JARVIS_SIBLING_MAX_RESAMPLE", "1")
    log: List[Any] = []
    first = _real_result([_shaped("a", 1)])
    out, n_calls = _run(
        first, [_syntax_exc(), _real_result([_shaped("b", 2)])],
        sampling_log=log,
    )
    assert n_calls == 2
    assert [c["candidate_hash"] for c in out.candidates] == ["a", "b"]
    assert log[1].temperature > log[0].temperature


def test_a_plain_exception_still_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the syntax contract is an answer; anything else is the lane."""
    monkeypatch.setenv(_ENV_N, "4")
    first = _real_result([_cand("a")])
    out, n_calls = _run(
        first, [RuntimeError("engine died"), _real_result([_cand("z")])],
    )
    assert [c["candidate_hash"] for c in out.candidates] == ["a"]
    assert n_calls == 1


def test_unparseable_at_the_cap_drops_the_slot_not_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ENV_N, "3")
    monkeypatch.setenv("JARVIS_SIBLING_MAX_RESAMPLE", "1")
    first = _real_result([_shaped("a", 1)])
    out, n_calls = _run(
        first,
        [_syntax_exc(), _syntax_exc(), _real_result([_shaped("c", 2)])],
    )
    assert n_calls == 3
    assert [c["candidate_hash"] for c in out.candidates] == ["a", "c"]


def test_collapsed_slots_feed_the_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two collapsed slots in a row; the third draw must sit on a HIGHER
    rung than a fresh slot would, once the multiplier is on. With it off
    (the default) the schedule is the plain ladder."""
    monkeypatch.setenv(_ENV_N, "4")
    monkeypatch.setenv("JARVIS_SIBLING_MAX_RESAMPLE", "0")   # one draw per slot
    first = _real_result([_shaped("a", 1)])

    def _drive() -> List[Any]:
        log: List[Any] = []
        _run(
            first,
            [_noop_result(), _noop_result(), _real_result([_shaped("d", 2)])],
            sampling_log=log,
        )
        return log

    monkeypatch.delenv("JARVIS_SIBLING_ESCALATION_MULTIPLIER", raising=False)
    off = _drive()
    monkeypatch.setenv("JARVIS_SIBLING_ESCALATION_MULTIPLIER", "2.0")
    on = _drive()
    assert len(off) == len(on) == 3
    # slot 2 (streak 0) identical either way; the multiplier never touches
    # a draw that has no collapse behind it.
    assert off[0] == on[0]
    # after two collapses the multiplied draw is hotter than the ladder's.
    assert on[2].temperature >= off[2].temperature
    assert on[2].seed != off[2].seed


def test_fulfillment_line_fires_for_a_singleton(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The telemetry: an op that lost every slot leaves a ledger, not silence.

    Before, the exit summary fired only when something merged, so a
    singleton was an ABSENCE in the log -- and 15 of them hid inside
    healthy-looking aggregates for a whole soak.
    """
    import logging

    monkeypatch.setenv(_ENV_N, "3")
    monkeypatch.setenv("JARVIS_SIBLING_MAX_RESAMPLE", "0")
    caplog.set_level(
        logging.INFO,
        logger="backend.core.ouroboros.governance.candidate_generator",
    )
    first = _real_result([_shaped("a", 1)])
    out, _ = _run(first, [_noop_result(), _syntax_exc()])
    assert out is first
    lines = [r.getMessage() for r in caplog.records
             if "sibling_fulfillment" in r.getMessage()]
    assert len(lines) == 1, lines
    line = lines[0]
    assert "wanted=2 got=0" in line
    assert "SINGLETON" in line
    assert "2:noop(dropped)" in line
    assert "3:unparseable(dropped)" in line
    assert "noops=1" in line and "unparseable=1" in line


def test_fulfillment_line_names_a_stop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    monkeypatch.setenv(_ENV_N, "4")
    caplog.set_level(
        logging.INFO,
        logger="backend.core.ouroboros.governance.candidate_generator",
    )
    first = _real_result([_shaped("a", 1)])
    _run(first, [_real_result([_shaped("b", 2)]), None])
    line = next(r.getMessage() for r in caplog.records
                if "sibling_fulfillment" in r.getMessage())
    assert "wanted=3 got=1" in line
    assert "2:merged" in line and "3:empty" in line and "4:not_attempted" in line
    assert "SINGLETON" not in line
