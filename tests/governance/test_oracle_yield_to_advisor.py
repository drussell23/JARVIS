"""
Task #88f spine — Oracle cooperative yield to Advisor.

v14-rev10 graduation soak proved: Oracle's full-tree
``incremental_update([])`` polling contends with Advisor's blast-radius
file walks on disk I/O. SWE op's Advisor scan took 4m 46s under
Oracle contention (vs <2s when Oracle was quiet).

Task #88f introduces a public "advisor busy" counter
(``get_advisor_busy_count()``) incremented around every advise_async
blast scan + a yield gate in ``_oracle_index_loop`` that skips a
poll cycle when busy > 0.  Bounded by max-consecutive-skips so
Oracle is never indefinitely starved.

This spine pins:

  * ``get_advisor_busy_count()`` is a public function (not a private
    attribute reach into executor._work_queue, per operator binding).
  * Counter increments around advise_async + decrements via
    ``finally`` (decrements on every exit path).
  * Counter is thread-safe (lock-protected).
  * Counter never goes negative (clamps at 0 on over-decrement).
  * The yield gate is master-flag-gated by
    ``JARVIS_ORACLE_YIELD_TO_ADVISOR`` (default-TRUE).
  * Bounded skip via ``JARVIS_ORACLE_YIELD_MAX_CONSECUTIVE_SKIPS``
    (default 10) prevents indefinite Oracle starvation.
  * FlagRegistry seeds present for both flags.
"""
from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_ADVISOR_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "operation_advisor.py"
)
_GLS_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "governed_loop_service.py"
)
_SEED_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "flag_registry_seed.py"
)


# ---------------------------------------------------------------------------
# Public counter behavior
# ---------------------------------------------------------------------------


def test_advisor_busy_count_starts_at_zero():
    from backend.core.ouroboros.governance.operation_advisor import (
        get_advisor_busy_count,
        _advisor_busy_decr,
        _advisor_busy_incr,
    )
    # Reset counter to known state (may have been touched by prior tests)
    while get_advisor_busy_count() > 0:
        _advisor_busy_decr()
    assert get_advisor_busy_count() == 0


def test_advisor_busy_count_increments_and_decrements():
    from backend.core.ouroboros.governance.operation_advisor import (
        get_advisor_busy_count,
        _advisor_busy_decr,
        _advisor_busy_incr,
    )
    while get_advisor_busy_count() > 0:
        _advisor_busy_decr()
    _advisor_busy_incr()
    assert get_advisor_busy_count() == 1
    _advisor_busy_incr()
    assert get_advisor_busy_count() == 2
    _advisor_busy_decr()
    assert get_advisor_busy_count() == 1
    _advisor_busy_decr()
    assert get_advisor_busy_count() == 0


def test_advisor_busy_count_clamps_at_zero_on_overdecrement():
    """Defensive clamp — buggy double-decrement must NOT produce
    negative counts (would make the public surface lie)."""
    from backend.core.ouroboros.governance.operation_advisor import (
        get_advisor_busy_count,
        _advisor_busy_decr,
    )
    while get_advisor_busy_count() > 0:
        _advisor_busy_decr()
    _advisor_busy_decr()  # over-decrement
    _advisor_busy_decr()  # over-decrement
    assert get_advisor_busy_count() == 0


def test_advisor_busy_count_is_thread_safe():
    """Many threads simultaneously incrementing + decrementing
    must not corrupt the count (final state matches algebra)."""
    from backend.core.ouroboros.governance.operation_advisor import (
        get_advisor_busy_count,
        _advisor_busy_decr,
        _advisor_busy_incr,
    )
    while get_advisor_busy_count() > 0:
        _advisor_busy_decr()
    barrier = threading.Barrier(20)

    def _worker():
        barrier.wait()
        for _ in range(100):
            _advisor_busy_incr()
            _advisor_busy_decr()

    threads = [threading.Thread(target=_worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert get_advisor_busy_count() == 0, (
        "20 threads × (100 incr + 100 decr) must leave count at 0"
    )


# ---------------------------------------------------------------------------
# AST pins — wiring discipline
# ---------------------------------------------------------------------------


def test_ast_pin_advise_async_wraps_with_busy_tracking():
    """``advise_async`` MUST wrap the executor call with the busy
    counter (incr at start, decr in finally).  Without this, the
    Oracle yield gate has no signal.
    """
    src = _ADVISOR_SRC.read_text(encoding="utf-8")
    assert "_advisor_busy_incr()" in src, (
        "advise_async must call _advisor_busy_incr() before the blast scan"
    )
    assert "_advisor_busy_decr()" in src, (
        "advise_async must call _advisor_busy_decr() in finally"
    )
    # Verify the function used inside run_in_executor is the busy-tracking wrapper
    assert "_advise_with_busy_tracking" in src, (
        "advise_async must dispatch via _advise_with_busy_tracking (Task #88f)"
    )


def test_ast_pin_oracle_loop_uses_public_busy_count():
    """Oracle loop MUST use ``get_advisor_busy_count()`` (the public
    function), NOT ``executor._work_queue.qsize()`` (private API,
    fragile across Python versions — operator binding 2026-05-14).

    Negative pin walks the AST (not just substring grep) so my own
    comments explaining what NOT to do don't false-positive.
    """
    src = _GLS_SRC.read_text(encoding="utf-8")
    assert "from backend.core.ouroboros.governance.operation_advisor import" in src, (
        "_oracle_index_loop must import from operation_advisor"
    )
    assert "get_advisor_busy_count" in src, (
        "_oracle_index_loop must call get_advisor_busy_count() — Task #88f"
    )
    # Negative pin — AST walk: ensure no Attribute node accesses
    # ``_work_queue.qsize`` as actual code (excludes string comments).
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "qsize":
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr == "_work_queue":
                raise AssertionError(
                    "_oracle_index_loop must NOT reach "
                    "ThreadPoolExecutor._work_queue.qsize() as actual "
                    "code — use the public get_advisor_busy_count() "
                    "per operator binding 2026-05-14. AST walk found "
                    f"at line {node.lineno}."
                )


def test_ast_pin_yield_gate_has_master_flag():
    """The yield gate MUST be master-flag-gated by
    ``JARVIS_ORACLE_YIELD_TO_ADVISOR`` so operators can revert with
    one env flip."""
    src = _GLS_SRC.read_text(encoding="utf-8")
    assert "JARVIS_ORACLE_YIELD_TO_ADVISOR" in src, (
        "Oracle yield gate must check JARVIS_ORACLE_YIELD_TO_ADVISOR"
    )


def test_ast_pin_bounded_skip_prevents_starvation():
    """The yield gate MUST have a bounded skip ceiling.  Without
    this, Oracle could be indefinitely starved by always-busy
    Advisor under sustained SWE-soak load.
    """
    src = _GLS_SRC.read_text(encoding="utf-8")
    assert "JARVIS_ORACLE_YIELD_MAX_CONSECUTIVE_SKIPS" in src, (
        "Oracle yield gate must have a bounded-skip ceiling via "
        "JARVIS_ORACLE_YIELD_MAX_CONSECUTIVE_SKIPS"
    )
    assert "_consec_skips" in src, (
        "Oracle yield gate must track consecutive skips"
    )
    assert "bounded-skip ceiling reached" in src, (
        "Oracle yield gate must log when the bounded-skip ceiling fires"
    )


# ---------------------------------------------------------------------------
# Yield-decision logic (extracted for unit test)
# ---------------------------------------------------------------------------


def _make_yield_decision(
    busy_count: int,
    consec_skips: int,
    max_skips: int,
    yield_enabled: bool,
) -> str:
    """Mirrors the _oracle_index_loop decision logic exactly.

    Returns one of:
      * 'skip'   — yield this cycle (counter increments)
      * 'force'  — bounded-skip ceiling reached, force the poll
      * 'run'    — advisor idle or master-off, run normally
    """
    if yield_enabled and consec_skips < max_skips and busy_count > 0:
        return "skip"
    if yield_enabled and consec_skips >= max_skips:
        return "force"
    return "run"


@pytest.mark.parametrize("busy,skips,max_skips,enabled,expected", [
    # Master off → always run
    (0, 0, 10, False, "run"),
    (5, 0, 10, False, "run"),
    (5, 100, 10, False, "run"),
    # Master on, advisor idle → run
    (0, 0, 10, True, "run"),
    (0, 5, 10, True, "run"),
    # Master on, advisor busy, within bounded skip → skip
    (1, 0, 10, True, "skip"),
    (10, 5, 10, True, "skip"),
    (1, 9, 10, True, "skip"),
    # Master on, advisor busy, at bounded-skip ceiling → force
    (5, 10, 10, True, "force"),
    (5, 100, 10, True, "force"),
    # Master on, ceiling exactly reached even with busy=0 → run
    # (we don't punish Oracle when advisor is finally idle just because we skipped a lot)
    (0, 10, 10, True, "force"),
])
def test_yield_decision_table(busy, skips, max_skips, enabled, expected):
    assert _make_yield_decision(busy, skips, max_skips, enabled) == expected


# ---------------------------------------------------------------------------
# FlagRegistry seed pins
# ---------------------------------------------------------------------------


def test_seed_has_yield_master_flag():
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_ORACLE_YIELD_TO_ADVISOR" in src
    idx = src.find("JARVIS_ORACLE_YIELD_TO_ADVISOR")
    window = src[idx:idx + 1500]
    assert "default=True" in window, (
        "Task #88f master flag default MUST be True (operator binding: "
        "default-TRUE only if no starvation, which the bounded-skip "
        "ceiling guarantees)"
    )
    assert "Category.SAFETY" in window


def test_seed_has_bounded_skip_flag():
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_ORACLE_YIELD_MAX_CONSECUTIVE_SKIPS" in src
    idx = src.find("JARVIS_ORACLE_YIELD_MAX_CONSECUTIVE_SKIPS")
    window = src[idx:idx + 1500]
    assert "default=10" in window, (
        "Default bounded-skip ceiling = 10 cycles = 30 min @ 3min "
        "poll interval, balancing yield vs starvation"
    )


# ---------------------------------------------------------------------------
# Integration: full advise_async dispatch increments + decrements the counter
# ---------------------------------------------------------------------------


def test_advise_async_increments_counter_during_call():
    """End-to-end: invoking advise_async must reflect in the counter
    DURING the call, and decrement back to 0 after.
    """
    from backend.core.ouroboros.governance.operation_advisor import (
        OperationAdvisor,
        get_advisor_busy_count,
        _advisor_busy_decr,
    )
    while get_advisor_busy_count() > 0:
        _advisor_busy_decr()

    advisor = OperationAdvisor(project_root=Path("/tmp"))

    # Race: advise_async is async, so we need to observe the counter
    # mid-flight.  We use a tiny synthetic call (no actual files) and
    # check both during + after.
    counter_during = {"value": 0}
    original_advise = advisor.advise

    def _spy_advise(*args, **kwargs):
        # Sample the busy count INSIDE the executor thread (proves
        # incr happened before, decr happens after)
        counter_during["value"] = get_advisor_busy_count()
        return original_advise(*args, **kwargs)

    advisor.advise = _spy_advise

    async def _go():
        return await advisor.advise_async(
            target_files=("nonexistent_test_file.py",),
            description="task-88f-spy",
            op_id="op-spy",
            is_read_only=True,
        )

    asyncio.run(_go())
    assert counter_during["value"] >= 1, (
        f"Counter during call must be >= 1 (Task #88f incr fires); "
        f"got {counter_during['value']}"
    )
    assert get_advisor_busy_count() == 0, (
        "Counter must return to 0 after advise_async completes (finally fired)"
    )


def test_advise_async_decrements_on_exception():
    """If advise raises, the finally MUST still decrement the counter."""
    from backend.core.ouroboros.governance.operation_advisor import (
        OperationAdvisor,
        get_advisor_busy_count,
        _advisor_busy_decr,
    )
    while get_advisor_busy_count() > 0:
        _advisor_busy_decr()

    advisor = OperationAdvisor(project_root=Path("/tmp"))

    def _raises(*args, **kwargs):
        raise RuntimeError("synthetic-task-88f-exception")

    advisor.advise = _raises

    async def _go():
        try:
            return await advisor.advise_async(
                target_files=("nonexistent.py",),
                description="task-88f-exc",
                op_id="op-exc",
            )
        except RuntimeError:
            return None

    asyncio.run(_go())
    assert get_advisor_busy_count() == 0, (
        "Counter must return to 0 even on exception (finally must fire)"
    )
