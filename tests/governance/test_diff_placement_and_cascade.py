"""A hunk that IS placeable must be placed, and a cascade must mean the
feedback failed — not that a budget was misread.

Measured on the local 30B, soak bt-2026-09-17-205140: 43% of its diffs were
declared malformed. The rejections read

    Diff hunk at line 1 does not match source — context diverges after 12
    matching line(s): expected '"port": port,', file line 26 is '        "port":'

The model stated line 1, the anchor sat at 25, and the anchored placement tier
refused to look past ±15. The hunks were correct and placeable; the SEARCH gave
up before reaching them.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import providers as P
from backend.core.ouroboros.governance import semantic_quality_observer as SQ


_HEADER = "".join(f"# header line {i}\n" for i in range(1, 22))
_BODY = '''
def build(port, hostname):
    return {
        "port": port,
        "hostname": hostname,
    }
'''
_ORIGINAL = _HEADER + _BODY


# --------------------------------------------------------------------------
# Placement — the root cause of the 43%
# --------------------------------------------------------------------------

def test_a_hunk_far_from_its_stated_line_is_still_placed():
    """THE regression: stated line 1, anchor at ~25, old window ±15."""
    diff = (
        "@@ -1,4 +1,5 @@\n"
        '         "port": port,\n'
        '+        "scheme": scheme,\n'
        '         "hostname": hostname,\n'
    )
    out = P._apply_unified_diff(_ORIGINAL, diff)
    assert '"scheme": scheme,' in out


def test_indentation_stripped_context_still_places():
    """The model drops leading whitespace from its context lines."""
    diff = (
        "@@ -1,4 +1,5 @@\n"
        '"port": port,\n'
        '+        "scheme": scheme,\n'
        '"hostname": hostname,\n'
    )
    assert '"scheme": scheme,' in P._apply_unified_diff(_ORIGINAL, diff)


def test_the_nearest_occurrence_still_wins():
    """Precision comes from the anchor SEQUENCE and nearest-first ordering, not
    from a bound: a file with the same anchor twice resolves to the one the
    model meant."""
    doubled = _ORIGINAL + _BODY
    stated_second = doubled[: doubled.index("def build", 40)].count("\n") + 4
    diff = (
        f"@@ -{stated_second},3 +{stated_second},4 @@\n"
        '         "port": port,\n'
        '+        "scheme": scheme,\n'
        '         "hostname": hostname,\n'
    )
    out = P._apply_unified_diff(doubled, diff)
    assert out.count('"scheme": scheme,') == 1
    # it edited the SECOND occurrence, the one the line number pointed at
    assert out.index('"scheme"') > out.index("def build", 40) - 400


def test_a_genuinely_absent_anchor_is_still_rejected():
    """Widening the search must not turn a wrong patch into an applied one."""
    diff = (
        "@@ -1,3 +1,4 @@\n"
        "         this_line_is_not_in_the_file()\n"
        "+        added()\n"
    )
    with pytest.raises(ValueError):
        P._apply_unified_diff(_ORIGINAL, diff)


def test_no_window_constant_bounds_the_anchored_tier():
    import inspect

    src = inspect.getsource(P._align_hunk)
    assert "lo, hi = 0, n" in src, "the anchored tier is bounded again"


def test_the_bounded_tiers_keep_their_window():
    """The earlier tiers compare a full text slice against a position, so a
    bound legitimately keeps them O(window)."""
    import inspect

    assert "window" in inspect.getsource(P._find_hunk_start)


# --------------------------------------------------------------------------
# The cascade — defined by its correction failing, not by a budget
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean():
    SQ.reset_for_tests()
    yield
    SQ.reset_for_tests()


def test_a_first_malformed_diff_never_sheds_the_op():
    """It is the first datum, and the realignment retry exists to answer it.
    The old version read a budget off ctx that never carried it, saw 0, and
    shed on a single failure: TerminalDiffCascade 7x, realignment 0x."""
    SQ.note_malformed("op-a")
    assert SQ.diff_cascade_exhausted_after_feedback("op-a") is False


def test_a_malformed_diff_AFTER_feedback_is_a_cascade():
    SQ.note_malformed("op-a")
    SQ.note_realignment_armed("op-a")
    assert SQ.diff_cascade_exhausted_after_feedback("op-a") is True


def test_ops_are_independent():
    SQ.note_realignment_armed("op-a")
    assert SQ.diff_cascade_exhausted_after_feedback("op-b") is False


def test_no_budget_or_threshold_is_consulted():
    """The budget read WAS the bug; the replacement must not reintroduce one."""
    import inspect

    body = inspect.getsource(SQ.diff_cascade_exhausted_after_feedback).split('"""')[-1]
    for suspect in ("retries", "budget", "max(", ">=", "threshold"):
        assert suspect not in body, f"a numeric ceiling crept back in: {suspect}"


def test_realignment_is_armed_only_on_a_successful_write():
    """Claiming the feedback landed when the ctx could not take it would shed
    an op that never received a correction."""
    class _Frozen:
        __slots__ = ()
        op_id = "op-frozen"

    P._arm_diff_context_realignment(_Frozen(), "x.py", "boom")
    assert SQ.diff_cascade_exhausted_after_feedback("op-frozen") is False


def test_a_writable_ctx_arms_and_carries_the_locator():
    class _Ctx:
        op_id = "op-w"
        strategic_memory_prompt = ""

    ctx = _Ctx()
    P._arm_diff_context_realignment(ctx, "x.py", "hunk 1: 'def foo' not found")
    assert "hunk 1: 'def foo' not found" in ctx.strategic_memory_prompt
    assert SQ.diff_cascade_exhausted_after_feedback("op-w") is True
