"""Subagent work, visible while it happens.

`SubagentOrchestrator` already emitted a lifecycle event for every dispatch —
`CommSink.emit_spawn`/`emit_result`, documented as "§7 Absolute observability:
every dispatch emits a spawn event" — and those reached CommProtocol as
HEARTBEAT frames with `phase="subagent_spawn"`.

Nothing rendered them. A grep for `subagent_spawn` across every cockpit
surface returned ZERO consumers, so an op that recruited four subagents and
ran them in parallel looked, from the operator's chair, exactly like an op
that had stalled. A live producer with no renderer — the same shape as the `/`
palette wired into a layout `ov` never mounted.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.subagent_narrator import (
    SubagentNarrationSink,
    narration_enabled,
    render_result,
    render_spawn,
)

_REPO = Path(__file__).resolve().parents[2]


class _Inner:
    def __init__(self) -> None:
        self.events: List[str] = []

    def emit_spawn(self, *_a: Any) -> None:
        self.events.append("spawn")

    def emit_result(self, *_a: Any) -> None:
        self.events.append("result")


class _Ok:
    ok = True
    files_touched = ["a", "b", "c"]
    findings: List[str] = []
    summary = "scanned"


# --------------------------------------------------------------------------
# 1. it reads like op chrome
# --------------------------------------------------------------------------

def test_a_spawn_opens_with_the_primary_action_glyph() -> None:
    """§04: ⏺ opens a primary action."""
    line = render_spawn("explore", "find every caller of _normalize")
    assert line.startswith("⏺ Explore(")
    assert "find every caller" in line


def test_a_result_is_subordinate_to_its_spawn() -> None:
    """§04: ⎿ is subordinate to the line above — which is what makes the pair
    read as one unit of work rather than two events."""
    assert render_result(_Ok(), 2.1).lstrip().startswith("⎿")
    assert render_result(_Ok(), 2.1).startswith("  ")


@pytest.mark.parametrize("token,spoken", [
    ("explore", "Explore"), ("review", "Review"),
    ("plan", "Plan"), ("general", "Agent"),
])
def test_each_kind_is_SPOKEN_not_tokenised(token: str, spoken: str) -> None:
    """The orchestrator's type is a routing identifier; this is the word an
    operator reads."""
    assert render_spawn(token, "x").startswith(f"⏺ {spoken}(")


def test_an_unknown_kind_still_renders() -> None:
    """A new subagent type must degrade to something readable, not blank."""
    assert "Synthesiz" in render_spawn("synthesize", "x")


def test_a_long_goal_is_clipped_to_one_line() -> None:
    line = render_spawn("explore", "word " * 80)
    assert len(line) < 100 and line.endswith(")")


def test_a_multiline_goal_is_flattened() -> None:
    """A newline inside chrome breaks the ⏺/⎿ pairing visually."""
    assert "\n" not in render_spawn("plan", "line one\nline two")


# --------------------------------------------------------------------------
# 2. the summary is a summary
# --------------------------------------------------------------------------

def test_zero_counts_are_omitted() -> None:
    """"0 findings" on an EXPLORE reports a metric belonging to REVIEW, and
    reads as a result rather than an absence."""
    assert "0 findings" not in render_result(_Ok(), 1.0)
    assert "3 files" in render_result(_Ok(), 1.0)


def test_a_failure_says_why() -> None:
    class _Fail:
        ok = False
        error = "provider timeout"

    out = render_result(_Fail(), 0.4)
    assert "failed" in out and "provider timeout" in out


def test_elapsed_is_shown_so_cost_is_legible() -> None:
    assert "2.1s" in render_result(_Ok(), 2.1)


def test_a_result_with_nothing_to_report_still_closes_the_pair() -> None:
    """An unterminated ⏺ reads as work still running."""
    class _Bare:
        ok = True

    assert "done" in render_result(_Bare(), None)


@pytest.mark.parametrize("junk", [None, 42, object(), "text"])
def test_rendering_never_raises(junk: Any) -> None:
    assert isinstance(render_result(junk, 1.0), str)
    assert isinstance(render_spawn(junk, "x"), str)


# --------------------------------------------------------------------------
# 3. it decorates — it does not replace
# --------------------------------------------------------------------------

def test_the_wrapped_sink_sees_every_event() -> None:
    """Swapping the CommProtocol sink would cost the spine carrying these
    events to the ledger, the observability API and the SSE stream."""
    inner = _Inner()
    sink = SubagentNarrationSink(inner, lambda _l: None)
    sink.emit_spawn("op-1", "sub-1", "explore", "goal")
    sink.emit_result("op-1", "sub-1", _Ok())
    assert inner.events == ["spawn", "result"]


def test_a_narration_fault_cannot_break_observability() -> None:
    """Delegation happens FIRST, so a rendering fault cannot starve the inner
    sink — observability is the contract that must not break."""
    inner = _Inner()

    def _boom(_line: str) -> None:
        raise RuntimeError("cockpit gone")

    sink = SubagentNarrationSink(inner, _boom)
    sink.emit_spawn("op-1", "sub-1", "explore", "goal")
    sink.emit_result("op-1", "sub-1", _Ok())
    assert inner.events == ["spawn", "result"]


def test_an_inner_fault_cannot_blank_the_cockpit() -> None:
    """The inverse: each failure is caught where it happens."""
    class _BrokenInner:
        def emit_spawn(self, *_a: Any) -> None:
            raise RuntimeError("comm down")

        def emit_result(self, *_a: Any) -> None:
            raise RuntimeError("comm down")

    lines: List[str] = []
    sink = SubagentNarrationSink(_BrokenInner(), lines.append)
    sink.emit_spawn("op-1", "sub-1", "explore", "goal")
    sink.emit_result("op-1", "sub-1", _Ok())
    assert len(lines) == 2


def test_unknown_methods_forward_to_the_wrapped_sink() -> None:
    """The CommSink protocol is structural and may grow; a decorator
    implementing only what it knew about would silently drop the next
    method added."""
    class _Rich:
        def emit_spawn(self, *_a: Any) -> None: ...
        def emit_result(self, *_a: Any) -> None: ...
        def emit_future_thing(self) -> str:
            return "forwarded"

    assert SubagentNarrationSink(_Rich()).emit_future_thing() == "forwarded"


def test_it_works_with_no_inner_sink_at_all() -> None:
    lines: List[str] = []
    SubagentNarrationSink(None, lines.append).emit_spawn(
        "op-1", "sub-1", "explore", "goal",
    )
    assert lines


def test_it_works_with_no_cockpit_attached() -> None:
    """Narration is skipped, dispatch is untouched — the daemon runs
    detached most of the time."""
    inner = _Inner()
    SubagentNarrationSink(inner).emit_spawn("op-1", "s", "explore", "g")
    assert inner.events == ["spawn"]


# --------------------------------------------------------------------------
# 4. elapsed is measured honestly
# --------------------------------------------------------------------------

def test_elapsed_is_measured_across_the_pair() -> None:
    lines: List[str] = []
    sink = SubagentNarrationSink(None, lines.append)
    sink.emit_spawn("op-1", "sub-1", "explore", "goal")
    time.sleep(0.05)
    sink.emit_result("op-1", "sub-1", _Ok())
    assert "s" in lines[1]


def test_a_result_without_a_spawn_omits_elapsed_rather_than_inventing_it():
    """A subagent whose spawn was missed must not be assigned a fabricated
    duration."""
    lines: List[str] = []
    SubagentNarrationSink(None, lines.append).emit_result(
        "op-1", "orphan", _Ok(),
    )
    assert "s" not in lines[0].replace("files", "")


def test_concurrent_subagents_do_not_share_a_clock() -> None:
    """Parallel dispatch is the normal case — one shared start time would
    give every agent the duration of the slowest."""
    lines: List[str] = []
    sink = SubagentNarrationSink(None, lines.append)
    sink.emit_spawn("op-1", "a", "explore", "g")
    time.sleep(0.05)
    sink.emit_spawn("op-1", "b", "review", "g")
    sink.emit_result("op-1", "b", _Ok())
    sink.emit_result("op-1", "a", _Ok())
    b_elapsed = lines[2]
    a_elapsed = lines[3]
    assert b_elapsed != a_elapsed


# --------------------------------------------------------------------------
# 5. wiring
# --------------------------------------------------------------------------

def test_the_master_switch_defaults_on() -> None:
    """It closes an observability gap; a dark default would leave it closed."""
    assert narration_enabled() is True


def test_the_switch_silences_narration_without_touching_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_SUBAGENT_NARRATION_ENABLED", "0")
    inner, lines = _Inner(), []
    sink = SubagentNarrationSink(inner, lines.append)
    sink.emit_spawn("op-1", "sub-1", "explore", "goal")
    assert lines == []
    assert inner.events == ["spawn"]


def test_the_orchestrator_is_wrapped_at_its_one_construction_site() -> None:
    src = (_REPO / "backend/core/ouroboros/governance/"
           "governed_loop_service.py").read_text()
    assert "_wrap_subagent_narration(self, _sub_comm)" in src


def test_the_mirror_is_resolved_LATE() -> None:
    """SerpentFlow attaches after this stack is built, so a handle captured
    at construction time would be None forever — the wired-but-inert shape."""
    src = (_REPO / "backend/core/ouroboros/governance/"
           "governed_loop_service.py").read_text()
    body = src.split("def _wrap_subagent_narration")[1][:900]
    assert 'getattr(gls, "_serpent_flow", None)' in body
    assert "_mirror_markup" in body


def test_narration_never_costs_observability() -> None:
    """The wrapper returns the inner sink unchanged on any failure."""
    src = (_REPO / "backend/core/ouroboros/governance/"
           "governed_loop_service.py").read_text()
    body = src.split("def _wrap_subagent_narration")[1][:900]
    assert "return inner" in body
