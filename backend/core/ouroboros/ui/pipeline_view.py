"""Every in-flight op as one live block: its pipeline, its agents, its verdicts.

The cockpit showed phase progress for the ONE op the status bar tracked; every
other op's progress was invisible until it ended. With the block protocol each
op owns a LIVE ``pipeline`` block that re-renders in place:

    ● TestFailure · tests/test_model_status_api.py
      ✓ classify  ✓ route  ✓ generate  ● validate  ○ gate  ○ apply  ○ verify
      ├ ● Venom · J-Prime · round 2 · Search("risk_tier")
      └ ● REVIEW subagent · reading 2 files

and, collapsed, one line (``○ TestFailure · validate``). The cockpit decides
which op is expanded (:func:`arrange`); the daemon sends both forms, so the
wire stays full-state.

The stages are DERIVED from the pipeline's own ``OperationPhase`` and
``PHASE_TRANSITIONS``, never retyped:

* a phase that transitions to itself is a RETRY of the phase that leads into
  it (``GENERATE_RETRY`` counts as ``generate ×2``);
* a phase some predecessor can skip (the predecessor also reaches one of its
  successors directly) is OPTIONAL and shown only once reached;
* phases with no outgoing forward transition are terminal and never shown.

Pure composition: no I/O, no clock. NEVER raises.
"""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "PipelineState", "Stage", "arrange", "collapsed_line", "expanded_lines",
    "live_share", "stage_of", "stages",
]


@dataclass(frozen=True)
class Stage:
    name: str           # OperationPhase name
    label: str          # what the strip prints
    optional: bool      # shown only once reached


def _label(phase_name: str) -> str:
    # CONTEXT_EXPANSION -> "context": the first word carries the meaning, and
    # the strip has one line for the whole pipeline.
    return phase_name.split("_", 1)[0].lower()


@functools.lru_cache(maxsize=1)
def _derived() -> Tuple[Tuple[Stage, ...], Dict[str, str]]:
    try:
        from backend.core.ouroboros.governance.op_context import (
            OperationPhase, PHASE_TRANSITIONS,
        )
    except Exception:  # noqa: BLE001
        return (), {}
    # Terminal phases have no way out; every phase can escape to one (the
    # table injects them), so they say nothing about the pipeline's shape.
    terminal = {p for p in OperationPhase if not PHASE_TRANSITIONS.get(p)}
    forward: Dict[Any, set] = {
        p: set(t) - terminal for p, t in PHASE_TRANSITIONS.items() if p not in terminal}
    retries = {p for p, t in forward.items() if p in t}
    retry_of: Dict[str, str] = {}
    for p in OperationPhase:              # earliest predecessor is the base
        for r in forward.get(p, set()) & retries:
            if p not in retries:
                retry_of.setdefault(r.name, p.name)
    optional = set()
    for p, t in forward.items():
        for q in t - retries:
            # Skippable: the predecessor reaches one of q's successors directly.
            if forward.get(q, set()) & (t - {q}):
                optional.add(q)
    for p, t in forward.items():
        # A tail extension: no forward successor of its own (only a retry of
        # an earlier phase or a terminal), so the op may end without it.
        if p not in retries and not (t - retries):
            optional.add(p)
    out = tuple(
        Stage(p.name, _label(p.name), p in optional)
        for p in OperationPhase if p in forward and p not in retries
    )
    return out, retry_of


def stages() -> Tuple[Stage, ...]:
    """The forward pipeline, in enum order. NEVER raises."""
    try:
        return _derived()[0]
    except Exception:  # noqa: BLE001
        return ()


def stage_of(phase_name: str) -> str:
    """The stage a phase counts toward: a retry counts toward its base."""
    try:
        name = str(phase_name or "").upper()
        return _derived()[1].get(name, name)
    except Exception:  # noqa: BLE001
        return str(phase_name or "").upper()


@dataclass
class PipelineState:
    """What the transport knows about one op, updated as events arrive."""

    op_id: str
    sensor: str = ""
    summary: str = ""
    phase: str = ""                                  # current stage name
    reached: List[str] = field(default_factory=list)  # stage names, in order
    attempts: Dict[str, int] = field(default_factory=dict)
    tool: str = ""                                   # in-flight tool label
    round_index: int = 0
    provider: str = ""                               # set while tokens stream
    verdict: str = ""                                # the gate's word

    def enter(self, phase_name: str) -> bool:
        """Record a transition. Returns whether anything visible changed."""
        stage = stage_of(phase_name)
        if not stage:
            return False
        self.attempts[stage] = self.attempts.get(stage, 0) + 1
        if stage not in self.reached:
            self.reached.append(stage)
        changed = stage != self.phase or self.attempts[stage] > 1
        self.phase = stage
        return changed


def _mark(name: str) -> str:
    try:
        from backend.core.ouroboros.ui.theme import mark
        return mark(name) or ""
    except Exception:  # noqa: BLE001
        return {"check": "+", "pending": "o", "action": "*",
                "branch": "|", "branch_last": "`", "dot": "-"}.get(name, "")


def _tint(text: str, tone: str) -> str:
    try:
        from backend.core.ouroboros.ui import deck_grammar as _deck
        style = _deck._style(tone)
        return f"[{style}]{text}[/{style}]" if style else text
    except Exception:  # noqa: BLE001
        return text


def _esc(text: Any) -> str:
    try:
        from backend.core.ouroboros.ui import deck_grammar as _deck
        return _deck._escape(text)
    except Exception:  # noqa: BLE001
        return str(text).replace("[", "\\[")


def _strip(state: PipelineState) -> str:
    cells: List[str] = []
    for st in stages():
        reached = st.name in state.reached
        if st.optional and not reached:
            continue
        n = state.attempts.get(st.name, 0)
        times = f" ×{n}" if n > 1 else ""
        if st.name == state.phase:
            cells.append(_tint(f"{_mark('action')} {st.label}{times}", "info"))
        elif reached:
            cells.append(_tint(f"{_mark('check')} {st.label}{times}", "ok"))
        else:
            cells.append(_tint(f"{_mark('pending')} {st.label}", "faint"))
    return "  ".join(cells)


def _head(state: PipelineState, *, tone: str) -> str:
    sep = _tint(_mark("dot"), "faint")
    lead = _tint(_mark("action"), tone)
    head = f"{lead} {_tint(_esc(state.sensor or 'Operation'), 'ink')}"
    if state.summary:
        head += f" {sep} {_tint(_esc(state.summary), 'muted')}"
    return head


def _agent_rows(state: PipelineState,
                agents: Sequence[Tuple[str, str]]) -> List[str]:
    rows: List[Tuple[str, str]] = []
    if state.provider or state.tool:
        parts = ["Venom"]
        if state.provider:
            parts.append(state.provider)
        if state.round_index:
            parts.append(f"round {state.round_index}")
        if state.tool:
            parts.append(state.tool)
        rows.append(("Venom", f" {_mark('dot')} ".join(parts[1:])))
    rows.extend(agents)
    out = []
    for i, (who, what) in enumerate(rows):
        branch = _mark("branch_last" if i == len(rows) - 1 else "branch")
        line = f"    {_tint(branch, 'faint')} {_tint(_mark('action'), 'info')} {_esc(who)}"
        if what:
            line += f" {_tint(_mark('dot'), 'faint')} {_tint(_esc(what), 'muted')}"
        out.append(line)
    return out


def expanded_lines(state: PipelineState,
                   agents: Sequence[Tuple[str, str]] = ()) -> Tuple[str, ...]:
    """The full block: header, stage strip, agent tree, verdict."""
    try:
        lines = [_head(state, tone="info"), "    " + _strip(state)]
        lines.extend(_agent_rows(state, agents))
        if state.verdict:
            lines.append(f"    {_tint('gate', 'faint')} {_tint(_esc(state.verdict), 'warn')}")
        return tuple(lines)
    except Exception:  # noqa: BLE001
        return (f"* {state.sensor} {state.phase}",)


def collapsed_line(state: PipelineState) -> str:
    """One line: ``○ TestFailure · summary · validate``."""
    try:
        sep = _tint(_mark("dot"), "faint")
        head = f"{_tint(_mark('pending'), 'faint')} {_tint(_esc(state.sensor or 'Operation'), 'ink')}"
        if state.summary:
            head += f" {sep} {_tint(_esc(state.summary), 'muted')}"
        phase = next((s.label for s in stages() if s.name == state.phase),
                     state.phase.lower())
        if phase:
            head += f" {sep} {_tint(phase, 'info')}"
        return head
    except Exception:  # noqa: BLE001
        return f"o {state.sensor} {state.phase}"


def live_share() -> float:
    """``JARVIS_COCKPIT_LIVE_SHARE``: the fraction of the terminal's rows the
    live area may take (default 0.4)."""
    try:
        return min(0.9, max(0.1, float(os.environ.get("JARVIS_COCKPIT_LIVE_SHARE", "") or 0.4)))
    except ValueError:
        return 0.4


def arrange(blocks: Iterable[Any], *, rows: Optional[int],
            focused_op: str = "") -> List[str]:
    """The live area's lines from the live blocks, by the density rules.

    ONE pipeline block is expanded: the focused op, else the op generating
    (it holds the model), else the newest. Every other pipeline collapses to
    its one line. Non-pipeline blocks (a tool call in flight) keep their own
    lines. Order is the daemon's ``seq``, so nothing jumps. Over the budget,
    the oldest collapsed rows give way to ``+N more · /agents``. NEVER raises.
    """
    try:
        blocks = list(blocks)
        pipes = [b for b in blocks if getattr(b, "kind", "") == "pipeline"]
        chosen = None
        if pipes:
            chosen = (next((b for b in pipes if b.op_id == focused_op), None)
                      or next((b for b in reversed(pipes) if b.meta.get("generating")), None)
                      or pipes[-1])
        out: List[List[str]] = []
        for b in blocks:
            if getattr(b, "kind", "") != "pipeline":
                out.append(list(b.lines))
            elif b is chosen:
                out.append(list(b.lines))
            else:
                out.append([str(b.meta.get("collapsed") or (b.lines[0] if b.lines else ""))])
        flat = [ln for group in out for ln in group]
        if rows is None:
            return flat
        budget = max(2, int(rows * live_share()))
        if len(flat) <= budget:
            return flat
        # Drop whole collapsed rows, oldest first; the expanded op and tool
        # blocks stay whole.
        hidden = 0
        for i, b in enumerate(blocks):
            if sum(len(g) for g in out) <= budget - 1:
                break
            if getattr(b, "kind", "") == "pipeline" and b is not chosen and out[i]:
                out[i] = []
                hidden += 1
        flat = [ln for group in out for ln in group]
        if hidden:
            flat.append(_tint(f"+{hidden} more {_mark('dot')} /agents", "faint"))
        return flat
    except Exception:  # noqa: BLE001
        return [ln for b in blocks for ln in getattr(b, "lines", ())]
