"""Move 6.5 Slice 5 — Canvas + diff-fan-out renderer.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "Fan-out UX: use OpBlockBuffer (and existing §37 Tier 2 #12
   fields) for sibling prior rolls before bespoke UI state.
   Slice 5 — Canvas + diff-fan-out renderer composes /canvas
   Tier 2 #12 + diff_preview.py — Per-prior diff overlay."

Pinned coverage (~30 tests):
  * Master flag default-FALSE per §33.1
  * record_for_canvas no-op when master off
  * DispatchVerdictRing bounded (deque maxlen) + drop-oldest
  * record_for_canvas appends + threads through OpBlockBuffer
  * find_recent hit + miss + ring eviction returns None
  * recent_verdicts limit + ordering
  * render_fan_out_overview: header + per-prior rows
  * render_fan_out_overview returns "" on None / empty rolls
  * render_diff_fan_out: composes diff_preview truncate +
    each prior surfaces its diff
  * render_diff_fan_out: empty diff handled
  * render_diff_fan_out: long diff truncated head/tail
  * 5 AST pins clean (parametrized) + each fires on synthetic
    regression
  * /canvas multi_prior <op_id>: bare hit + missing op_id +
    blank op_id + master-off message
  * /canvas multi_prior_diff <op_id>: same
  * /canvas help text mentions multi_prior subcommands
  * Public API surface complete + register_flags + swallows
    registry errors
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/verification/"
        "multi_prior_canvas.py"
    )


def _enable_all_masters(monkeypatch):
    for k in (
        "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED",
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED",
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED",
        "JARVIS_MULTI_PRIOR_CANVAS_ENABLED",
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED",
    ):
        monkeypatch.setenv(k, "true")


@pytest.fixture(autouse=True)
def _reset_ring():
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        reset_default_ring_for_test,
    )
    reset_default_ring_for_test()
    yield
    reset_default_ring_for_test()


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_CANVAS_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        master_enabled,
    )
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv(
            "JARVIS_MULTI_PRIOR_CANVAS_ENABLED", v,
        )
        assert master_enabled() is True


def test_record_noop_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_CANVAS_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        record_for_canvas, get_default_ring,
    )
    out = record_for_canvas(MagicMock())
    assert out is False
    assert len(get_default_ring()) == 0


def test_find_recent_returns_none_when_master_off(
    monkeypatch,
):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_CANVAS_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        find_recent,
    )
    assert find_recent("op-1") is None


def test_ring_size_clamped(monkeypatch):
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        ring_size,
    )
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_CANVAS_RING_SIZE", "0",
    )
    assert ring_size() == 1
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_CANVAS_RING_SIZE", "9999",
    )
    assert ring_size() == 200
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_CANVAS_RING_SIZE", "junk",
    )
    assert ring_size() == 30


# ---------------------------------------------------------------------------
# DispatchVerdictRing
# ---------------------------------------------------------------------------


def test_ring_bounded_drop_oldest():
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        DispatchVerdictRing,
    )
    r = DispatchVerdictRing(size=3)
    for i in range(5):
        v = MagicMock()
        v.op_id = f"op-{i}"
        r.append(v)
    snap = r.recent()
    assert len(snap) == 3
    assert [v.op_id for v in snap] == [
        "op-2", "op-3", "op-4",
    ]


def test_ring_find_recent_hit_miss():
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        DispatchVerdictRing,
    )
    r = DispatchVerdictRing(size=10)
    for i in range(3):
        v = MagicMock()
        v.op_id = f"op-{i}"
        r.append(v)
    assert r.find_recent("op-1").op_id == "op-1"
    assert r.find_recent("op-99") is None
    assert r.find_recent("") is None


def test_ring_recent_limit():
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        DispatchVerdictRing,
    )
    r = DispatchVerdictRing(size=10)
    for i in range(5):
        v = MagicMock()
        v.op_id = f"op-{i}"
        r.append(v)
    snap = r.recent(limit=2)
    assert [v.op_id for v in snap] == ["op-3", "op-4"]


def test_ring_append_none_silently_skipped():
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        DispatchVerdictRing,
    )
    r = DispatchVerdictRing(size=3)
    r.append(None)
    assert len(r) == 0


# ---------------------------------------------------------------------------
# record_for_canvas — composes OpBlockBuffer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_for_canvas_appends_to_ring(
    monkeypatch,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        dispatch_multi_prior,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        find_recent, record_for_canvas,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return f"diff-{prior.prior_id}"

    v = await dispatch_multi_prior(
        gen, op_id="op-A",
        route="complex", posture="EXPLORE",
    )
    assert record_for_canvas(v) is True
    found = find_recent("op-A")
    assert found is not None
    assert getattr(found, "op_id") == "op-A"


@pytest.mark.asyncio
async def test_record_for_canvas_calls_register_parent(
    monkeypatch,
):
    """Operator binding: the K rolls MUST appear in
    OpBlockBuffer's canonical fan-out tracker via
    register_parent. Slice 5's substrate composes;
    no parallel parent state."""
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        dispatch_multi_prior,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        record_for_canvas,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "x"

    v = await dispatch_multi_prior(
        gen, op_id="op-B",
        route="complex", posture="EXPLORE",
    )

    fake_buffer = MagicMock()
    record_for_canvas(v, op_block_buffer=fake_buffer)
    # K=4 rolls → 4 register_parent calls
    assert fake_buffer.register_parent.call_count == 4
    # Each call carries the canonical Tier 2 #12 fields
    for i, call in enumerate(
        fake_buffer.register_parent.call_args_list,
    ):
        kwargs = call.kwargs
        assert kwargs["parent_op_id"] == "op-B"
        assert kwargs["candidate_index"] == i
        assert kwargs["subagent_kind"] == "multi_prior"


def test_record_returns_false_on_none_verdict(
    monkeypatch,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        record_for_canvas,
    )
    assert record_for_canvas(None) is False


# ---------------------------------------------------------------------------
# render_fan_out_overview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_overview_contains_header_rows(
    monkeypatch,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        dispatch_multi_prior,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        render_fan_out_overview,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return f"x-{prior.prior_id}"

    v = await dispatch_multi_prior(
        gen, op_id="op-O",
        route="complex", posture="EXPLORE",
    )
    txt = render_fan_out_overview(v)
    # Header rows
    assert "/canvas multi_prior op-O:" in txt
    assert "decision=enabled" in txt
    assert "action=escalate_to_operator_review" in txt
    assert "consensus=disagreement" in txt
    # Per-prior rows (all 4 priors)
    assert "prior_id=seed_only:0" in txt
    assert "prior_id=style_hint:defensive" in txt
    assert "prior_id=style_hint:minimalist" in txt
    assert "prior_id=style_hint:composition_first" in txt


def test_render_overview_empty_on_none():
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        render_fan_out_overview,
    )
    assert render_fan_out_overview(None) == ""


def test_render_overview_empty_when_no_verdict_result():
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        render_fan_out_overview,
    )

    class Bad:
        op_id = "x"
        decision = MagicMock()
        action_recommendation = MagicMock()
        verdict_result = None
        roll_to_prior_id = {}

    assert render_fan_out_overview(Bad()) == ""


# ---------------------------------------------------------------------------
# render_diff_fan_out — composes diff_preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_diff_fan_out_per_prior_diffs(
    monkeypatch,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        dispatch_multi_prior,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        render_diff_fan_out,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return (
            "def x():\n    return " + repr(prior.prior_id)
        )

    v = await dispatch_multi_prior(
        gen, op_id="op-D",
        route="complex", posture="EXPLORE",
    )
    txt = render_diff_fan_out(v)
    # Header from overview
    assert "/canvas multi_prior op-D:" in txt
    # Diff fan-out section
    assert "diff fan-out:" in txt
    # Each prior's diff surfaces verbatim (short diffs)
    assert "return 'seed_only:0'" in txt
    assert "return 'style_hint:defensive'" in txt
    assert "return 'style_hint:minimalist'" in txt
    assert "return 'style_hint:composition_first'" in txt


@pytest.mark.asyncio
async def test_render_diff_fan_out_truncates_long_diff(
    monkeypatch,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        dispatch_multi_prior,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        render_diff_fan_out,
    )

    long_diff = "\n".join(
        f"line-{i}: x" for i in range(200)
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return long_diff

    v = await dispatch_multi_prior(
        gen, op_id="op-T",
        route="complex", posture="EXPLORE",
    )
    txt = render_diff_fan_out(v)
    assert "elided" in txt


def test_render_diff_fan_out_empty_on_none():
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        render_diff_fan_out,
    )
    assert render_diff_fan_out(None) == ""


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "multi_prior_canvas_master_default_false",
        "multi_prior_canvas_authority_asymmetry",
        "multi_prior_canvas_composes_op_block_buffer",
        "multi_prior_canvas_composes_diff_preview",
        "multi_prior_canvas_ring_bounded",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    assert pin.validate(tree, src) == ()


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_canvas_authority_asymmetry"
        )
    )
    assert pin.validate(tree, bad)


def test_op_block_buffer_pin_fires_when_register_missing():
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def _register_priors_in_op_block_buffer(verdict, *, op_block_buffer):
    pass  # no register_parent call
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_canvas_composes_op_block_buffer"
        )
    )
    assert pin.validate(tree, bad)


def test_diff_preview_pin_fires_when_import_missing():
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def _resolve_truncate_helper():
    def _local(text, *, max_lines, head_tail):
        return text
    return _local
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_canvas_composes_diff_preview"
        )
    )
    assert pin.validate(tree, bad)


def test_ring_bounded_pin_fires_on_unbounded_deque():
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class DispatchVerdictRing:
    def __init__(self, *, size=None):
        from collections import deque
        self._ring = deque()  # unbounded — forbidden
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_canvas_ring_bounded"
        )
    )
    assert pin.validate(tree, bad)


# ---------------------------------------------------------------------------
# /canvas multi_prior subcommand
# ---------------------------------------------------------------------------


def test_canvas_help_mentions_multi_prior():
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas help")
    assert "multi_prior" in out.text
    assert "multi_prior_diff" in out.text


def test_canvas_multi_prior_missing_op_id():
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas multi_prior")
    assert out.ok is False
    assert "missing op-id" in out.text.lower()


def test_canvas_multi_prior_diff_missing_op_id():
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command(
        "/canvas multi_prior_diff",
    )
    assert out.ok is False
    assert "missing op-id" in out.text.lower()


def test_canvas_multi_prior_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_CANVAS_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command(
        "/canvas multi_prior op-X",
    )
    assert out.ok is True
    assert "disabled" in out.text


def test_canvas_multi_prior_op_not_in_ring(monkeypatch):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command(
        "/canvas multi_prior nonexistent-op",
    )
    assert out.ok is True
    assert "not in the in-memory ring" in out.text


@pytest.mark.asyncio
async def test_canvas_multi_prior_full_render(
    monkeypatch,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        dispatch_multi_prior,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        record_for_canvas,
    )
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return f"diff-{prior.prior_id}"

    v = await dispatch_multi_prior(
        gen, op_id="op-X",
        route="complex", posture="EXPLORE",
    )
    record_for_canvas(v)
    out = dispatch_canvas_command(
        "/canvas multi_prior op-X",
    )
    assert out.ok is True
    assert "/canvas multi_prior op-X:" in out.text
    assert "prior_id=" in out.text


@pytest.mark.asyncio
async def test_canvas_multi_prior_diff_full_render(
    monkeypatch,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        dispatch_multi_prior,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        record_for_canvas,
    )
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return f"diff for {prior.prior_id}"

    v = await dispatch_multi_prior(
        gen, op_id="op-Y",
        route="complex", posture="EXPLORE",
    )
    record_for_canvas(v)
    out = dispatch_canvas_command(
        "/canvas multi_prior_diff op-Y",
    )
    assert out.ok is True
    assert "diff fan-out:" in out.text
    assert "diff for seed_only:0" in out.text


# ---------------------------------------------------------------------------
# Public API + register_flags
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance.verification import (  # noqa: E501
        multi_prior_canvas as mod,
    )
    expected = {
        "MULTI_PRIOR_CANVAS_SCHEMA_VERSION",
        "DispatchVerdictRing",
        "find_recent",
        "get_default_ring",
        "master_enabled",
        "recent_verdicts",
        "record_for_canvas",
        "register_flags",
        "register_shipped_invariants",
        "render_diff_fan_out",
        "render_fan_out_overview",
        "reset_default_ring_for_test",
        "ring_size",
    }
    assert set(mod.__all__) == expected


def test_register_flags_seeds_two():
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    register_flags(registry)
    assert registry.register.call_count == 2
    names = {
        c.kwargs["name"]
        for c in registry.register.call_args_list
    }
    assert names == {
        "JARVIS_MULTI_PRIOR_CANVAS_ENABLED",
        "JARVIS_MULTI_PRIOR_CANVAS_RING_SIZE",
    }


def test_register_flags_swallows_errors():
    from backend.core.ouroboros.governance.verification.multi_prior_canvas import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    registry.register.side_effect = RuntimeError("boom")
    register_flags(registry)
