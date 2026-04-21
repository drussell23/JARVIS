"""Tests for layout_repl (Slice 3)."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.layout_controller import (
    LayoutController,
    MODE_FLOW,
    MODE_SPLIT,
)
from backend.core.ouroboros.battle_test.layout_repl import (
    LayoutDispatchResult,
    dispatch_layout_command,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _c(initial: str = MODE_FLOW) -> LayoutController:
    return LayoutController(initial_mode=initial)


# ===========================================================================
# Match + no-match paths
# ===========================================================================


def test_non_layout_line_does_not_match():
    c = _c()
    res = dispatch_layout_command("/session help", controller=c)
    assert res.matched is False


def test_empty_line_does_not_match():
    c = _c()
    res = dispatch_layout_command("", controller=c)
    assert res.matched is False


# ===========================================================================
# Default entry — status
# ===========================================================================


def test_bare_layout_shows_status():
    c = _c(MODE_SPLIT)
    res = dispatch_layout_command("/layout", controller=c)
    assert res.ok
    assert "current mode" in res.text
    assert "split" in res.text


def test_status_lists_valid_regions():
    c = _c(MODE_FLOW)
    res = dispatch_layout_command("/layout", controller=c)
    for region in ("stream", "dashboard", "diff"):
        assert region in res.text


# ===========================================================================
# /layout help
# ===========================================================================


def test_help_lists_all_verbs():
    c = _c()
    res = dispatch_layout_command("/layout help", controller=c)
    assert res.ok
    for verb in ("flow", "split", "focus"):
        assert verb in res.text


def test_help_via_question_mark():
    c = _c()
    res = dispatch_layout_command("/layout ?", controller=c)
    assert res.ok
    assert "flow" in res.text


# ===========================================================================
# /layout flow — single escape verb
# ===========================================================================


def test_flow_from_split():
    c = _c(MODE_SPLIT)
    res = dispatch_layout_command("/layout flow", controller=c)
    assert res.ok
    assert c.mode == "flow"


def test_flow_from_focus():
    c = _c("focus:diff")
    res = dispatch_layout_command("/layout flow", controller=c)
    assert res.ok
    assert c.mode == "flow"


def test_flow_idempotent():
    c = _c(MODE_FLOW)
    res = dispatch_layout_command("/layout flow", controller=c)
    assert res.ok
    assert c.mode == "flow"


# ===========================================================================
# /layout split
# ===========================================================================


def test_split_activates():
    c = _c(MODE_FLOW)
    res = dispatch_layout_command("/layout split", controller=c)
    assert res.ok
    assert c.mode == "split"


def test_split_mentions_target_mode_in_text():
    c = _c(MODE_FLOW)
    res = dispatch_layout_command("/layout split", controller=c)
    assert "split" in res.text


# ===========================================================================
# /layout focus <region>
# ===========================================================================


@pytest.mark.parametrize("region", ["stream", "dashboard", "diff"])
def test_focus_each_valid_region(region: str):
    c = _c(MODE_FLOW)
    res = dispatch_layout_command(
        f"/layout focus {region}", controller=c,
    )
    assert res.ok
    assert c.mode == f"focus:{region}"


def test_focus_with_prefixed_form():
    c = _c(MODE_FLOW)
    res = dispatch_layout_command(
        "/layout focus focus:stream", controller=c,
    )
    assert res.ok
    assert c.mode == "focus:stream"


def test_focus_missing_region_returns_usage():
    c = _c(MODE_FLOW)
    res = dispatch_layout_command("/layout focus", controller=c)
    assert not res.ok
    assert "focus" in res.text
    assert "stream" in res.text  # usage mentions valid regions


def test_focus_unknown_region_is_error():
    c = _c(MODE_FLOW)
    res = dispatch_layout_command(
        "/layout focus evil", controller=c,
    )
    assert not res.ok
    assert "evil" in res.text


def test_focus_case_insensitive_region():
    c = _c(MODE_FLOW)
    res = dispatch_layout_command(
        "/layout focus STREAM", controller=c,
    )
    assert res.ok
    assert c.mode == "focus:stream"


# ===========================================================================
# Unknown verbs — fail closed
# ===========================================================================


def test_unknown_verb_returns_error():
    c = _c(MODE_FLOW)
    res = dispatch_layout_command("/layout evil", controller=c)
    assert not res.ok
    assert "unknown" in res.text.lower()


def test_malformed_quoting_reports_parse_error():
    c = _c(MODE_FLOW)
    res = dispatch_layout_command(
        "/layout 'unclosed quote", controller=c,
    )
    assert not res.ok
    assert "parse" in res.text.lower()


# ===========================================================================
# Result shape
# ===========================================================================


def test_result_dataclass_shape():
    res = LayoutDispatchResult(ok=True, text="hi")
    assert res.matched is True  # default
