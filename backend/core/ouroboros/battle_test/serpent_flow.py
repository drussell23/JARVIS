"""Serpent Flow — Ouroboros Flowing CLI with Organism Personality.

Layout Architecture (post UI Slice 3, 2026-04-30):

  Zone 0: Boot Banner — printed once at startup, scrolls away inline
  Zone 1: Event Log (scrolling) — all non-prompt rich output goes here
  Zone 2: Active Operation — current op header + status line (live-updated)
  Zone 3: Tool Stream — live tool-call output during Venom rounds
  Zone 4: Prompt Gate — inline prompt/diff/approval rendered in-flow (NEW Slice 5)
  Zone 5: REPL Bar — static bottom prompt bar (always visible)

Key design principles:
  - NO persistent Live() layout — causes terminal corruption with mixed print/rich
  - All zone output uses Console.print() directly for clean inline flow
  - Zone 2/3 use escape-code cursor tricks for in-place updates
  - Zone 4 renders as rich inline panels, then scrolls naturally into Zone 1 history
  - Three-channel terminal muting: Live TUI uses stderr; Zone output uses stdout
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import sys
import textwrap
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

from rich.columns import Columns
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.rule import Rule
from rich.style import Style
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

_PALETTE = {
    "soul": "#C850C0",
    "mind": "#4158D0",
    "body": "#0093E9",
    "gold": "#FFD700",
    "emerald": "#50C878",
    "crimson": "#DC143C",
    "amber": "#FFBF00",
    "frost": "#B0E0E6",
    "void": "#1a1a2e",
    "ghost": "#666680",
    "death": "#8B0000",
    "life": "#228B22",
    "warn": "#FF8C00",
}

_JARVIS_THEME = Theme(
    {
        "jarvis.soul": _PALETTE["soul"],
        "jarvis.mind": _PALETTE["mind"],
        "jarvis.body": _PALETTE["body"],
        "jarvis.gold": _PALETTE["gold"],
        "jarvis.emerald": _PALETTE["emerald"],
        "jarvis.crimson": _PALETTE["crimson"],
        "jarvis.amber": _PALETTE["amber"],
        "jarvis.frost": _PALETTE["frost"],
        "jarvis.ghost": _PALETTE["ghost"],
        "jarvis.warn": _PALETTE["warn"],
    }
)

_C = _PALETTE  # short alias


# ---------------------------------------------------------------------------
# Shared console (stdout)
# ---------------------------------------------------------------------------

console = Console(theme=_JARVIS_THEME, highlight=False)
err_console = Console(stderr=True, theme=_JARVIS_THEME, highlight=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _dim(s: str) -> str:
    return f"[dim]{escape(s)}[/dim]"


def _markup_safe(s: str) -> str:
    return escape(str(s))


# ---------------------------------------------------------------------------
# Zone 0 — Boot Banner
# ---------------------------------------------------------------------------

_BANNER_ART = r"""
     ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
     ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
     ██║███████║██████╔╝██║   ██║██║███████╗
██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
 ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝
"""

_SUBTITLE = "Ouroboros  ·  Self-Evolving Intelligence  ·  v3.0"


def print_boot_banner() -> None:
    """Zone 0: print the ASCII boot banner once, inline."""
    grad = [_PALETTE["soul"], _PALETTE["mind"], _PALETTE["body"]]
    lines = _BANNER_ART.strip("\n").splitlines()
    n = max(len(lines), 1)
    for i, line in enumerate(lines):
        t = i / max(n - 1, 1)
        # simple two-stop gradient pick
        if t < 0.5:
            color = grad[0] if t < 0.25 else grad[1]
        else:
            color = grad[1] if t < 0.75 else grad[2]
        console.print(f"[bold {color}]{line}[/bold {color}]", justify="center")
    console.print(f"[dim]{_SUBTITLE}[/dim]", justify="center")
    console.print()


# ---------------------------------------------------------------------------
# Zone 1 helpers — event log
# ---------------------------------------------------------------------------


def log_event(msg: str, *, style: str = "") -> None:
    """Append a timestamped line to the event log (Zone 1)."""
    ts = _dim(f"[{_ts()}]")
    if style:
        console.print(f"{ts} [{style}]{escape(msg)}[/{style}]")
    else:
        console.print(f"{ts} {escape(msg)}")


def log_separator(label: str = "") -> None:
    if label:
        console.print(Rule(f"[dim]{escape(label)}[/dim]", style="dim"))
    else:
        console.print(Rule(style="dim"))


# ---------------------------------------------------------------------------
# Zone 2 — Active Operation display
# ---------------------------------------------------------------------------


class ActiveOpDisplay:
    """In-place single-line active-operation status (Zone 2)."""

    def __init__(self) -> None:
        self._active = False
        self._op_id: str = ""
        self._description: str = ""

    def start(self, op_id: str, description: str) -> None:
        self._op_id = op_id[:12]
        self._description = description
        self._active = True
        self._render()

    def update(self, status: str) -> None:
        if not self._active:
            return
        console.print(
            f"  [{_C['body']}]⟳[/{_C['body']}] [{_C['ghost']}]{escape(self._op_id)}[/{_C['ghost']}]"
            f" {escape(status)}",
            highlight=False,
        )

    def finish(self, outcome: str = "done", *, error: bool = False) -> None:
        if not self._active:
            return
        color = _C["crimson"] if error else _C["emerald"]
        icon = "✗" if error else "✓"
        console.print(
            f"  [{color}]{icon}[/{color}] [{_C['ghost']}]{escape(self._op_id)}[/{_C['ghost']}]"
            f" [{color}]{escape(outcome)}[/{color}]",
            highlight=False,
        )
        self._active = False

    def _render(self) -> None:
        desc_short = self._description[:72] + ("…" if len(self._description) > 72 else "")
        console.print(
            f"\n[bold {_C['gold']}]⊕ OP[/bold {_C['gold']}]"
            f" [{_C['ghost']}]{escape(self._op_id)}[/{_C['ghost']}]"
            f"  {escape(desc_short)}",
            highlight=False,
        )


active_op = ActiveOpDisplay()


# ---------------------------------------------------------------------------
# Zone 3 — Tool stream
# ---------------------------------------------------------------------------


class ToolStreamDisplay:
    """Live tool-call output panel (Zone 3)."""

    def __init__(self, max_lines: int = 12) -> None:
        self._buf: deque[str] = deque(maxlen=max_lines)
        self._round = 0

    def new_round(self, round_num: int) -> None:
        self._round = round_num
        self._buf.clear()
        console.print(
            f"  [{_C['frost']}]── tool round {round_num} ──[/{_C['frost']}]",
            highlight=False,
        )

    def tool_call(self, name: str, args_preview: str = "") -> None:
        preview = args_preview[:60] + ("…" if len(args_preview) > 60 else "")
        line = f"  [{_C['amber']}]▶[/{_C['amber']}] {escape(name)}"
        if preview:
            line += f"  [{_C['ghost']}]{escape(preview)}[/{_C['ghost']}]"
        console.print(line, highlight=False)
        self._buf.append(name)

    def tool_result(self, name: str, result_preview: str = "", *, error: bool = False) -> None:
        color = _C["crimson"] if error else _C["emerald"]
        icon = "✗" if error else "◀"
        preview = result_preview[:80] + ("…" if len(result_preview) > 80 else "")
        line = f"  [{color}]{icon}[/{color}] {escape(name)}"
        if preview:
            line += f"  [{_C['ghost']}]{escape(preview)}[/{_C['ghost']}]"
        console.print(line, highlight=False)


tool_stream = ToolStreamDisplay()


# ---------------------------------------------------------------------------
# Zone 4 — Inline Prompt Gate renderer  (NEW — Slice 5)
# ---------------------------------------------------------------------------


class InlinePromptRenderer:
    """
    Zone 4: Renders approval prompts, diff previews, and ask_human questions
    as rich inline panels that flow naturally into Zone 1 history after display.

    This replaces the previous approach of injecting into the Live TUI layout,
    which caused terminal corruption. Instead we render inline, print a separator,
    and let the content scroll naturally.

    Integration points (wired in SerpentFlow.attach_phase_boundary_renderer):
      - NOTIFY_APPLY diff preview  →  render_diff_preview()
      - APPROVAL_REQUIRED prompt   →  render_approval_prompt()
      - ask_human clarification    →  render_ask_human()
      - post-op outcome summary    →  render_outcome_summary()
    """

    # -- colour shortcuts ------------------------------------------------
    _APPROVE_COLOR = _PALETTE["emerald"]
    _DENY_COLOR = _PALETTE["crimson"]
    _ASK_COLOR = _PALETTE["amber"]
    _DIFF_ADD = _PALETTE["emerald"]
    _DIFF_DEL = _PALETTE["crimson"]
    _DIFF_HDR = _PALETTE["frost"]

    def __init__(self) -> None:
        self._rendered_count = 0

    # ------------------------------------------------------------------
    # Public rendering API
    # ------------------------------------------------------------------

    def render_diff_preview(
        self,
        op_id: str,
        file_path: str,
        diff_text: str,
        *,
        risk_tier: str = "NOTIFY_APPLY",
        countdown_s: int = 5,
    ) -> None:
        """Render a colourised diff preview panel (NOTIFY_APPLY auto-apply countdown)."""
        self._rendered_count += 1
        console.print()
        console.print(
            Rule(
                f"[bold {_C['amber']}]  DIFF PREVIEW[/bold {_C['amber']}]"
                f"  [{_C['ghost']}]{escape(op_id[:16])}[/{_C['ghost']}]",
                style=_C["amber"],
            )
        )
        # File header
        console.print(
            f"[{_C['frost']}]  {escape(file_path)}[/{_C['frost']}]"
            f"  [dim]{escape(risk_tier)}[/dim]",
            highlight=False,
        )
        # Diff lines
        for raw_line in diff_text.splitlines()[:120]:
            self._print_diff_line(raw_line)
        if diff_text.count("\n") > 120:
            console.print(f"  [{_C['ghost']}]… (truncated)[/{_C['ghost']}]", highlight=False)
        console.print(
            f"\n  [{_C['amber']}]Auto-applying in {countdown_s}s — /cancel {escape(op_id)} to abort[/{_C['amber']}]",
            highlight=False,
        )
        console.print(Rule(style=_C["amber"]))
        console.print()

    def render_approval_prompt(
        self,
        op_id: str,
        file_path: str,
        diff_text: str,
        *,
        risk_tier: str = "APPROVAL_REQUIRED",
        rationale: str = "",
    ) -> None:
        """Render an orange-tier approval prompt panel."""
        self._rendered_count += 1
        console.print()
        console.print(
            Rule(
                f"[bold {_C['warn']}]  APPROVAL REQUIRED[/bold {_C['warn']}]"
                f"  [{_C['ghost']}]{escape(op_id[:16])}[/{_C['ghost']}]",
                style=_C["warn"],
            )
        )
        console.print(
            f"  [{_C['frost']}]{escape(file_path)}[/{_C['frost']}]"
            f"  [dim]{escape(risk_tier)}[/dim]",
            highlight=False,
        )
        if rationale:
            console.print(
                f"\n  [{_C['amber']}]Rationale:[/{_C['amber']}] {escape(rationale[:200])}",
                highlight=False,
            )
        # Diff
        for raw_line in diff_text.splitlines()[:80]:
            self._print_diff_line(raw_line)
        if diff_text.count("\n") > 80:
            console.print(f"  [{_C['ghost']}]… (truncated)[/{_C['ghost']}]", highlight=False)
        # Decision prompt
        console.print(
            f"\n  [bold]Commands:[/bold]"
            f"  [{_C['emerald']}]approve {escape(op_id)}[/{_C['emerald']}]"
            f"  [{_C['crimson']}]reject {escape(op_id)}[/{_C['crimson']}]"
            f"  [{_C['amber']}]cancel {escape(op_id)}[/{_C['amber']}]",
            highlight=False,
        )
        console.print(Rule(style=_C["warn"]))
        console.print()

    def render_ask_human(
        self,
        op_id: str,
        question: str,
        *,
        context: str = "",
    ) -> None:
        """Render an ask_human clarification panel."""
        self._rendered_count += 1
        console.print()
        console.print(
            Rule(
                f"[bold {_C['amber']}]  CLARIFICATION REQUEST[/bold {_C['amber']}]"
                f"  [{_C['ghost']}]{escape(op_id[:16])}[/{_C['ghost']}]",
                style=_C["amber"],
            )
        )
        console.print(
            Panel(
                f"[bold]{escape(question)}[/bold]"
                + (f"\n\n[dim]{escape(context)}[/dim]" if context else ""),
                border_style=_C["amber"],
                padding=(1, 2),
            )
        )
        console.print(
            f"  [{_C['ghost']}]Reply with:[/bold]"
            f"  [bold]reply {escape(op_id)} <your answer>[/bold]",
            highlight=False,
        )
        console.print(Rule(style=_C["amber"]))
        console.print()

    def render_outcome_summary(
        self,
        op_id: str,
        outcome: str,
        *,
        file_path: str = "",
        risk_tier: str = "",
        commit_sha: str = "",
        error_msg: str = "",
        duration_s: float = 0.0,
    ) -> None:
        """Render a post-op outcome summary panel."""
        self._rendered_count += 1
        success = outcome in ("COMPLETE", "APPLIED", "VERIFIED")
        color = _C["emerald"] if success else _C["crimson"]
        icon = "✓" if success else "✗"
        console.print()
        console.print(
            Rule(
                f"[bold {color}]{icon} {escape(outcome)}[/bold {color}]"
                f"  [{_C['ghost']}]{escape(op_id[:16])}[/{_C['ghost']}]",
                style=color,
            )
        )
        rows: list[tuple[str, str]] = []
        if file_path:
            rows.append(("File", file_path))
        if risk_tier:
            rows.append(("Risk", risk_tier))
        if commit_sha:
            rows.append(("Commit", commit_sha[:10]))
        if duration_s:
            rows.append(("Duration", f"{duration_s:.1f}s"))
        if error_msg:
            rows.append(("Error", error_msg[:120]))
        if rows:
            tbl = Table(show_header=False, box=None, padding=(0, 2))
            tbl.add_column(style=f"dim {_C['ghost']}", no_wrap=True)
            tbl.add_column()
            for k, v in rows:
                tbl.add_row(k, escape(v))
            console.print(tbl)
        console.print(Rule(style=color))
        console.print()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _print_diff_line(self, line: str) -> None:
        if line.startswith("+++") or line.startswith("---"):
            console.print(
                f"  [{self._DIFF_HDR}]{escape(line)}[/{self._DIFF_HDR}]",
                highlight=False,
            )
        elif line.startswith("+"):
            console.print(
                f"  [{self._DIFF_ADD}]{escape(line)}[/{self._DIFF_ADD}]",
                highlight=False,
            )
        elif line.startswith("-"):
            console.print(
                f"  [{self._DIFF_DEL}]{escape(line)}[/{self._DIFF_DEL}]",
                highlight=False,
            )
        elif line.startswith("@@"):
            console.print(
                f"  [{_C['frost']}]{escape(line)}[/{_C['frost']}]",
                highlight=False,
            )
        else:
            console.print(f"  {escape(line)}", highlight=False)


# Module-level singleton
inline_prompt_renderer = InlinePromptRenderer()


# ---------------------------------------------------------------------------
# Update block (CC-style flowing diff)
# ---------------------------------------------------------------------------

_DIFF_ADD_COLOR = _PALETTE["emerald"]
_DIFF_DEL_COLOR = _PALETTE["crimson"]
_DIFF_HDR_COLOR = _PALETTE["frost"]


@dataclass
class _UpdateBlock:
    path: str
    op_id: str
    diff_lines: list[str] = field(default_factory=list)
    reasoning: str = ""
    risk_tier: str = ""
    outcome: str = ""


def _render_update_block(blk: _UpdateBlock) -> None:
    """Render a single Update(path) block with colourised diff (Zone 1)."""
    title = (
        f"[bold {_C['gold']}]Update[/bold {_C['gold']}]"
        f"([{_C['frost']}]{escape(blk.path)}[/{_C['frost']}])"
        f"  [{_C['ghost']}]{escape(blk.op_id[:12])}[/{_C['ghost']}]"
    )
    if blk.risk_tier:
        tier_color = {
            "SAFE_AUTO": _C["emerald"],
            "NOTIFY_APPLY": _C["amber"],
            "APPROVAL_REQUIRED": _C["warn"],
            "BLOCKED": _C["crimson"],
        }.get(blk.risk_tier, _C["ghost"])
        title += f"  [{tier_color}]{escape(blk.risk_tier)}[/{tier_color}]"
    if blk.outcome:
        out_color = _C["emerald"] if blk.outcome in ("COMPLETE", "APPLIED") else _C["crimson"]
        title += f"  [{out_color}]{escape(blk.outcome)}[/{out_color}]"

    body_parts: list[str] = []
    if blk.reasoning:
        body_parts.append(f"[dim italic]{escape(blk.reasoning[:200])}[/dim italic]")
        body_parts.append("")

    for line in blk.diff_lines[:80]:
        if line.startswith("+++") or line.startswith("---"):
            body_parts.append(
                f"[{_DIFF_HDR_COLOR}]{escape(line)}[/{_DIFF_HDR_COLOR}]"
            )
        elif line.startswith("+"):
            body_parts.append(f"[{_DIFF_ADD_COLOR}]{escape(line)}[/{_DIFF_ADD_COLOR}]")
        elif line.startswith("-"):
            body_parts.append(f"[{_DIFF_DEL_COLOR}]{escape(line)}[/{_DIFF_DEL_COLOR}]")
        elif line.startswith("@@"):
            body_parts.append(f"[{_C['frost']}]{escape(line)}[/{_C['frost']}]")
        else:
            body_parts.append(escape(line))

    if len(blk.diff_lines) > 80:
        body_parts.append(f"[{_C['ghost']}]… +{len(blk.diff_lines)-80} lines[/{_C['ghost']}]")

    body = "\n".join(body_parts)
    console.print(Panel(body, title=title, border_style=_C["ghost"], padding=(0, 1)))


# ---------------------------------------------------------------------------
# Token stream renderer  (Zone 2 / 3 augmentation)
# ---------------------------------------------------------------------------


class StreamRenderer:
    """Consume raw token chunks and render them to the console.

    Behaviour differs by TTY vs headless:
      - TTY: live spinner + partial line rewrite
      - Headless: quiet (only completed content logged)
    """

    _TTY = sys.stdout.isatty()
    _MAX_LINE = 120

    def __init__(self) -> None:
        self._buf: list[str] = []
        self._spinner: Progress | None = None
        self._task_id: Any = None

    def start(self, label: str = "Generating…") -> None:
        if self._TTY:
            self._spinner = Progress(
                SpinnerColumn(spinner_name="dots", style=f"bold {_C['soul']}"),
                TextColumn(f"[{_C['ghost']}]{escape(label)}[/{_C['ghost']}]"),
                transient=True,
                console=console,
            )
            self._spinner.start()
            self._task_id = self._spinner.add_task(label, total=None)

    def token(self, chunk: str) -> None:
        self._buf.append(chunk)

    def finish(self) -> str:
        if self._spinner:
            self._spinner.stop()
            self._spinner = None
        content = "".join(self._buf)
        self._buf.clear()
        return content

    def flush_to_log(self, content: str, *, label: str = "Generated") -> None:
        if not content:
            return
        lines = content.splitlines()
        preview = " ".join(lines[:3])[:160]
        log_event(f"{label}: {preview}{'…' if len(lines) > 3 else ''}", style="dim")


stream_renderer = StreamRenderer()


# ---------------------------------------------------------------------------
# Metrics sparkline
# ---------------------------------------------------------------------------


def _sparkline(values: list[float], *, width: int = 20) -> str:
    if not values:
        return " " * width
    _BLOCKS = "▁▂▃▄▅▆▇█"
    mn, mx = min(values), max(values)
    rng = mx - mn or 1.0
    result = []
    for v in values[-width:]:
        idx = int((v - mn) / rng * (len(_BLOCKS) - 1))
        result.append(_BLOCKS[idx])
    return "".join(result)


# ---------------------------------------------------------------------------
# REPL input bar (Zone 5)
# ---------------------------------------------------------------------------


class ReplBar:
    """Zone 5: static bottom REPL prompt bar."""

    PROMPT = f"[bold {_C['soul']}]jarvis>[/bold {_C['soul']}] "

    def print_prompt(self) -> None:
        console.print(self.PROMPT, end="")

    def print_help(self) -> None:
        tbl = Table(show_header=False, box=None, padding=(0, 2))
        tbl.add_column(style=f"bold {_C['amber']}", no_wrap=True)
        tbl.add_column(style="dim")
        cmds = [
            ("status", "show current op + recent events"),
            ("ops", "list all active operations"),
            ("cancel <op-id>", "cooperative cancel of an operation"),
            ("approve <op-id>", "approve an APPROVAL_REQUIRED operation"),
            ("reject <op-id>", "reject an APPROVAL_REQUIRED operation"),
            ("reply <op-id> <text>", "answer an ask_human question"),
            ("vision", "vision sensor status"),
            ("posture", "show/override strategic posture"),
            ("governor", "sensor governor status"),
            ("help [flags|flag NAME|verbs]", "flag registry help"),
            ("metrics", "show performance metrics"),
            ("quit / exit", "graceful shutdown"),
        ]
        for cmd, desc in cmds:
            tbl.add_row(cmd, desc)
        console.print(
            Panel(tbl, title=f"[bold {_C['gold']}]REPL Commands[/bold {_C['gold']}]", border_style=_C["ghost"])
        )


repl_bar = ReplBar()


# ---------------------------------------------------------------------------
# SerpentFlow  — top-level coordinator
# ---------------------------------------------------------------------------


class SerpentFlow:
    """
    Top-level CLI coordinator for the Ouroboros battle-test harness.

    Responsibilities:
      - Boot banner + initial status
      - Route inbound events (op lifecycle, tool calls, prompts) to the
        correct zone renderer
      - REPL input loop (Zone 5)
      - Phase-boundary hook attachment (Zone 4 — Slice 5b)

    All rendering is delegated to zone singletons; SerpentFlow only wires
    them together and maintains session-level counters.
    """

    def __init__(
        self,
        *,
        governed_loop: Any = None,
        cost_cap: float = 1.0,
        headless: bool = False,
    ) -> None:
        self.governed_loop = governed_loop
        self.cost_cap = cost_cap
        self.headless = headless

        # Session counters
        self._ops_completed = 0
        self._ops_failed = 0
        self._total_cost = 0.0
        self._session_start = time.monotonic()

        # Zone 4 renderer (inline prompt gate)
        self._prompt_renderer = inline_prompt_renderer

        # REPL command registry {verb: handler}
        self._repl_handlers: dict[str, Callable[..., Coroutine[Any, Any, None]]] = {}

        # Subscriber handles for cleanup
        self._unsub_handles: list[Any] = []

        # Inline prompt gate subscription handle (Slice 5b)
        self._unsub_inline_prompt_renderer: Any = None

        # Phase boundary renderer attachment flag
        self._phase_boundary_attached = False

    # ------------------------------------------------------------------
    # Boot
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Boot sequence: banner, status, attach hooks."""
        if not self.headless:
            print_boot_banner()

        log_separator("Ouroboros Battle Test — Session Start")
        log_event(
            f"Session started  cost_cap=${self.cost_cap:.2f}  headless={self.headless}",
            style=f"{_C['ghost']}",
        )

        # Wire Zone 4 (Slice 5b)
        self.attach_phase_boundary_renderer()

        if self.governed_loop is not None:
            await self._attach_loop_callbacks()

    async def stop(self) -> None:
        """Graceful teardown: detach hooks, print session summary."""
        self._detach_all()
        elapsed = time.monotonic() - self._session_start
        log_separator("Session End")
        log_event(
            f"ops={self._ops_completed + self._ops_failed}"
            f" ok={self._ops_completed} fail={self._ops_failed}"
            f" cost=${self._total_cost:.4f}"
            f" elapsed={elapsed:.0f}s",
            style=_C["ghost"],
        )

    # ------------------------------------------------------------------
    # Zone 4 wire-up (Slice 5b)
    # ------------------------------------------------------------------

    def attach_phase_boundary_renderer(self) -> None:
        """
        Wire InlinePromptRenderer into the governed loop's event bus so that
        phase-boundary events (NOTIFY_APPLY diff, APPROVAL_REQUIRED, ask_human,
        outcome summaries) are rendered inline in Zone 4.

        Called once during start().  Safe to call again — idempotent.

        The governed loop exposes an event bus via:
            governed_loop.event_bus.subscribe(event_type, callback)
        returning an unsubscribe handle (callable or object with .cancel()).

        Supported event types (string constants from governance/events.py):
            "diff_preview"        → render_diff_preview
            "approval_required"   → render_approval_prompt
            "ask_human"           → render_ask_human
            "op_outcome"          → render_outcome_summary

        If the governed loop is not yet set or does not expose an event_bus,
        this is a no-op (graceful degradation — Zone 4 renders nothing until
        the loop is attached later via set_governed_loop()).
        """
        if self._phase_boundary_attached:
            return  # idempotent

        if self.governed_loop is None:
            # Will be wired later when loop is attached
            return

        bus = getattr(self.governed_loop, "event_bus", None)
        if bus is None:
            # Governed loop exists but has no event bus — log and skip
            log_event(
                "InlinePromptGate: governed_loop has no event_bus — Zone 4 inactive",
                style=_C["ghost"],
            )
            return

        subscribe = getattr(bus, "subscribe", None)
        if subscribe is None:
            log_event(
                "InlinePromptGate: event_bus has no subscribe() — Zone 4 inactive",
                style=_C["ghost"],
            )
            return

        # --- diff_preview --------------------------------------------------
        def _on_diff_preview(evt: dict[str, Any]) -> None:
            try:
                self._prompt_renderer.render_diff_preview(
                    op_id=evt.get("op_id", "?"),
                    file_path=evt.get("file_path", "?"),
                    diff_text=evt.get("diff_text", ""),
                    risk_tier=evt.get("risk_tier", "NOTIFY_APPLY"),
                    countdown_s=int(evt.get("countdown_s", 5)),
                )
            except Exception as exc:  # noqa: BLE001
                log_event(f"InlinePromptGate diff_preview error: {exc}", style=_C["warn"])

        # --- approval_required ---------------------------------------------
        def _on_approval_required(evt: dict[str, Any]) -> None:
            try:
                self._prompt_renderer.render_approval_prompt(
                    op_id=evt.get("op_id", "?"),
                    file_path=evt.get("file_path", "?"),
                    diff_text=evt.get("diff_text", ""),
                    risk_tier=evt.get("risk_tier", "APPROVAL_REQUIRED"),
                    rationale=evt.get("rationale", ""),
                )
            except Exception as exc:  # noqa: BLE001
                log_event(f"InlinePromptGate approval_required error: {exc}", style=_C["warn"])

        # --- ask_human -----------------------------------------------------
        def _on_ask_human(evt: dict[str, Any]) -> None:
            try:
                self._prompt_renderer.render_ask_human(
                    op_id=evt.get("op_id", "?"),
                    question=evt.get("question", ""),
                    context=evt.get("context", ""),
                )
            except Exception as exc:  # noqa: BLE001
                log_event(f"InlinePromptGate ask_human error: {exc}", style=_C["warn"])

        # --- op_outcome ----------------------------------------------------
        def _on_op_outcome(evt: dict[str, Any]) -> None:
            try:
                self._prompt_renderer.render_outcome_summary(
                    op_id=evt.get("op_id", "?"),
                    outcome=evt.get("outcome", "UNKNOWN"),
                    file_path=evt.get("file_path", ""),
                    risk_tier=evt.get("risk_tier", ""),
                    commit_sha=evt.get("commit_sha", ""),
                    error_msg=evt.get("error_msg", ""),
                    duration_s=float(evt.get("duration_s", 0.0)),
                )
            except Exception as exc:  # noqa: BLE001
                log_event(f"InlinePromptGate op_outcome error: {exc}", style=_C["warn"])

        # Subscribe all four event types; store handles for cleanup
        handles: list[Any] = []
        for event_type, handler in [
            ("diff_preview", _on_diff_preview),
            ("approval_required", _on_approval_required),
            ("ask_human", _on_ask_human),
            ("op_outcome", _on_op_outcome),
        ]:
            try:
                handle = subscribe(event_type, handler)
                handles.append(handle)
            except Exception as exc:  # noqa: BLE001
                log_event(
                    f"InlinePromptGate subscribe({event_type}) failed: {exc}",
                    style=_C["warn"],
                )

        if handles:
            # Store as a single aggregate handle that cancels all four
            self._unsub_inline_prompt_renderer = handles
            self._unsub_handles.extend(handles)
            self._phase_boundary_attached = True
            log_event(
                f"InlinePromptGate: Zone 4 attached ({len(handles)}/4 event types wired)",
                style=_C["ghost"],
            )
        else:
            log_event(
                "InlinePromptGate: no event types could be subscribed — Zone 4 inactive",
                style=_C["warn"],
            )

    def set_governed_loop(self, loop: Any) -> None:
        """
        Late-bind the governed loop after construction.

        Useful when SerpentFlow is instantiated before the GovernedLoopService
        is ready (common in harness boot order).  Triggers attach_phase_boundary_renderer()
        if not already attached.
        """
        self.governed_loop = loop
        if not self._phase_boundary_attached:
            self.attach_phase_boundary_renderer()

    # ------------------------------------------------------------------
    # Op lifecycle callbacks (wired from governed loop)
    # ------------------------------------------------------------------

    async def _attach_loop_callbacks(self) -> None:
        """Register callbacks on the governed loop for op lifecycle events."""
        loop = self.governed_loop
        if loop is None:
            return

        # on_op_start
        if hasattr(loop, "register_op_start_callback"):
            loop.register_op_start_callback(self._on_op_start)

        # on_op_complete
        if hasattr(loop, "register_op_complete_callback"):
            loop.register_op_complete_callback(self._on_op_complete)

        # on_tool_call
        if hasattr(loop, "register_tool_call_callback"):
            loop.register_tool_call_callback(self._on_tool_call)

        # on_tool_result
        if hasattr(loop, "register_tool_result_callback"):
            loop.register_tool_result_callback(self._on_tool_result)

    def _on_op_start(self, op_id: str, description: str, **_: Any) -> None:
        active_op.start(op_id, description)

    def _on_op_complete(
        self,
        op_id: str,
        outcome: str,
        *,
        error: bool = False,
        cost: float = 0.0,
        **_: Any,
    ) -> None:
        active_op.finish(outcome, error=error)
        if error:
            self._ops_failed += 1
        else:
            self._ops_completed += 1
        self._total_cost += cost

    def _on_tool_call(self, round_num: int, tool_name: str, args_preview: str = "", **_: Any) -> None:
        tool_stream.new_round(round_num)
        tool_stream.tool_call(tool_name, args_preview)

    def _on_tool_result(
        self, tool_name: str, result_preview: str = "", *, error: bool = False, **_: Any
    ) -> None:
        tool_stream.tool_result(tool_name, result_preview, error=error)

    # ------------------------------------------------------------------
    # REPL
    # ------------------------------------------------------------------

    def register_repl_handler(
        self, verb: str, handler: Callable[..., Coroutine[Any, Any, None]]
    ) -> None:
        self._repl_handlers[verb] = handler

    async def run_repl(self) -> None:
        """Zone 5: async REPL input loop."""
        if self.headless:
            # In headless mode, just wait forever (shutdown via event)
            try:
                await asyncio.get_event_loop().create_future()
            except asyncio.CancelledError:
                pass
            return

        repl_bar.print_help()
        loop = asyncio.get_event_loop()

        while True:
            try:
                repl_bar.print_prompt()
                raw = await loop.run_in_executor(None, sys.stdin.readline)
                if not raw:  # EOF
                    break
                line = raw.strip()
                if not line:
                    continue
                if line in ("quit", "exit"):
                    log_event("Shutdown requested via REPL.", style=_C["amber"])
                    break
                await self._dispatch_repl(line)
            except (KeyboardInterrupt, asyncio.CancelledError):
                break
            except EOFError:
                break

    async def _dispatch_repl(self, line: str) -> None:
        parts = line.split(None, 1)
        verb = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if verb in self._repl_handlers:
            try:
                await self._repl_handlers[verb](args)
            except Exception as exc:  # noqa: BLE001
                log_event(f"REPL handler error ({verb}): {exc}", style=_C["crimson"])
            return

        # Built-in fallbacks
        if verb == "status":
            await self._cmd_status()
        elif verb == "metrics":
            await self._cmd_metrics()
        elif verb == "help":
            repl_bar.print_help()
        else:
            log_event(f"Unknown command: {escape(verb)}  (type help for list)", style=_C["ghost"])

    async def _cmd_status(self) -> None:
        elapsed = time.monotonic() - self._session_start
        tbl = Table(show_header=False, box=None, padding=(0, 2))
        tbl.add_column(style=f"dim {_C['ghost']}", no_wrap=True)
        tbl.add_column()
        rows = [
            ("ops ok", str(self._ops_completed)),
            ("ops fail", str(self._ops_failed)),
            ("cost", f"${self._total_cost:.4f} / ${self.cost_cap:.2f}"),
            ("elapsed", f"{elapsed:.0f}s"),
            ("zone4 renders", str(self._prompt_renderer._rendered_count)),
        ]
        for k, v in rows:
            tbl.add_row(k, v)
        console.print(
            Panel(tbl, title=f"[bold {_C['gold']}]Session Status[/bold {_C['gold']}]", border_style=_C["ghost"])
        )

    async def _cmd_metrics(self) -> None:
        log_event("metrics: (no time-series data available in this session)", style=_C["ghost"])

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _detach_all(self) -> None:
        """Cancel all subscription handles registered during start()."""
        for handle in self._unsub_handles:
            if callable(handle):
                with contextlib.suppress(Exception):
                    handle()
            elif hasattr(handle, "cancel"):
                with contextlib.suppress(Exception):
                    handle.cancel()
        self._unsub_handles.clear()
        self._unsub_inline_prompt_renderer = None
        self._phase_boundary_attached = False


# ---------------------------------------------------------------------------
# Convenience factory used by the harness
# ---------------------------------------------------------------------------


def create_serpent_flow(
    *,
    governed_loop: Any = None,
    cost_cap: float = 1.0,
    headless: bool | None = None,
) -> SerpentFlow:
    """
    Factory used by ouroboros_battle_test harness.

    headless defaults to True when stdin is not a TTY (CI / daemon), matching
    the --headless / --no-headless flag behaviour documented in CLAUDE.md.
    """
    if headless is None:
        headless = not sys.stdin.isatty()
    return SerpentFlow(
        governed_loop=governed_loop,
        cost_cap=cost_cap,
        headless=headless,
    )


# ---------------------------------------------------------------------------
# __all__
# ---------------------------------------------------------------------------

__all__ = [
    # Zone singletons
    "console",
    "err_console",
    "active_op",
    "tool_stream",
    "stream_renderer",
    "repl_bar",
    "inline_prompt_renderer",
    # Zone 0
    "print_boot_banner",
    # Zone 1
    "log_event",
    "log_separator",
    # Update block
    "_render_update_block",
    "_UpdateBlock",
    # Top-level coordinator
    "SerpentFlow",
    "create_serpent_flow",
]


# ---------------------------------------------------------------------------
# Standalone smoke-test  (python3 -m backend.core.ouroboros.battle_test.serpent_flow)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio

    async def _smoke() -> None:
        sf = create_serpent_flow(headless=True, cost_cap=0.10)
        await sf.start()

        # Simulate events
        active_op.start("op-abc123", "Refactor authentication module")
        active_op.update("CLASSIFY → ROUTE")
        active_op.update("GENERATE (round 1)")
        tool_stream.new_round(1)
        tool_stream.tool_call("read_file", "backend/core/ouroboros/orchestrator.py")
        tool_stream.tool_result("read_file", "4321 lines read")
        tool_stream.tool_call("search_code", "class OrchestratorFSM")
        tool_stream.tool_result("search_code", "3 matches")
        active_op.finish("COMPLETE")

        # Zone 4 — simulate events directly
        inline_prompt_renderer.render_diff_preview(
            op_id="op-abc123",
            file_path="backend/core/ouroboros/orchestrator.py",
            diff_text="--- a/orchestrator.py\n+++ b/orchestrator.py\n@@ -1,3 +1,4 @@\n+# new line\n existing line\n",
            countdown_s=5,
        )
        inline_prompt_renderer.render_approval_prompt(
            op_id="op-def456",
            file_path="backend/core/auth/login.py",
            diff_text="--- a/login.py\n+++ b/login.py\n@@ -10,2 +10,3 @@\n+    log.audit('login')\n",
            rationale="Added audit logging for compliance.",
        )
        inline_prompt_renderer.render_ask_human(
            op_id="op-ghi789",
            question="Should I also update the test fixtures for this change?",
            context="The current test uses hardcoded credentials.",
        )
        inline_prompt_renderer.render_outcome_summary(
            op_id="op-abc123",
            outcome="COMPLETE",
            file_path="backend/core/ouroboros/orchestrator.py",
            risk_tier="SAFE_AUTO",
            commit_sha="a1b2c3d4e5f6",
            duration_s=12.4,
        )

        await sf.stop()

    asyncio.run(_smoke())