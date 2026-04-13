"""LiveDashboard — Persistent Rich Live TUI for the Ouroboros battle test.

Replaces the scrolling log with an in-place updating dashboard that shows
the organism's state at a glance. Like htop for a self-developing AI.

Layout:
┌──────────────────────────────────────────────────────────────┐
│ OUROBOROS + VENOM            Session: bt-...  Branch: ...    │
│ Budget: [$████████░░] $0.32/$0.50    Elapsed: 4m 23s         │
├────────────────────────────────┬─────────────────────────────┤
│ Active Operations (3)         │ Organism Intelligence        │
│ ┌─────┬────────┬──────┬────┐  │ Triage:   12 screened        │
│ │ ID  │ Phase  │ Prov │ ⏱  │  │  ├─ 8 PROCEED, 3 NO_OP       │
│ ├─────┼────────┼──────┼────┤  │  └─ 1 REDIRECT               │
│ │ a3f │ GEN    │ DW   │ 12s│  │ Discovery: 5 intents sub'd   │
│ │ b72 │ VALID  │ CL   │ 8s │  │ Dreams: 2 blueprints         │
│ │ c91 │ APPLY  │ DW   │ 3s │  │ Learning: 4 rules, trend: ↑  │
│ └─────┴────────┴──────┴────┘  │ Self-Evo: 15 ops, 80% ✓      │
├───────────────────────────────┴──────────────────────────────┤
│ Event Log                                                    │
│ 12:34:05 ✨ GENERATE  op:a3f2  DW 397B + 6 Venom tools       │
│ 12:34:02 🔍 TRIAGE    op:b728  PROCEED (0.85 confidence)     │
│ 12:33:58 🧬 DISCOVERY cycle 3  submitted 4 intents           │
│ 12:33:55 ✅ COMPLETE  op:d4e1  2 files changed ($0.04)       │
│ 12:33:50 🧪 VALIDATE  op:c912  8 tests passed ✓              │
├──────────────────────────────────────────────────────────────┤
│ [Ctrl+C: stop] [d: diffs] [e: expand] Budget: DW $0.12 CL…   │
└──────────────────────────────────────────────────────────────┘

Manifesto §7: Absolute Observability — the inner workings of the
symbiote must be entirely visible.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
import warnings
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

# ══════════════════════════════════════════════════════════════
# Data models for dashboard state
# ══════════════════════════════════════════════════════════════

_PHASE_ICONS: Dict[str, str] = {
    "classify": "🔍", "route": "🧭", "context_expansion": "📚",
    "semantic_triage": "🧠", "generate": "✨", "validate": "🧪",
    "gate": "🛡️", "approve": "👤", "apply": "💾",
    "verify": "✅", "complete": "🎉",
}

_PHASE_COLORS: Dict[str, str] = {
    "classify": "blue", "route": "blue", "context_expansion": "cyan",
    "semantic_triage": "magenta", "generate": "yellow", "validate": "green",
    "gate": "cyan", "approve": "white", "apply": "green",
    "verify": "green", "complete": "bold green",
}

_PROVIDER_SHORT: Dict[str, str] = {
    "doubleword-397b": "DW 397B",
    "doubleword": "DW 397B",
    "claude-api": "Claude",
    "claude": "Claude",
    "gcp-jprime": "J-Prime",
}

_TOOL_ICONS: Dict[str, str] = {
    "read_file": "📄",
    "search_code": "🔍",
    "run_tests": "🧪",
    "bash": "💻",
    "web_search": "🌐",
    "web_fetch": "🌐",
    "get_callers": "🔗",
    "list_symbols": "📋",
    "glob_files": "📁",
    "list_dir": "📂",
    "git_log": "📜",
    "git_diff": "📊",
    "git_blame": "🔎",
    "edit_file": "✏️",
    "write_file": "📝",
    "code_explore": "🧪",
}

_TOOL_START_VERBS: Dict[str, str] = {
    "read_file": "reading",
    "search_code": "searching",
    "run_tests": "testing",
    "bash": "running",
    "web_search": "searching web",
    "web_fetch": "fetching",
    "get_callers": "finding callers",
    "list_symbols": "listing symbols",
    "glob_files": "globbing",
    "list_dir": "listing",
    "git_log": "checking git log",
    "git_diff": "diffing",
    "git_blame": "blaming",
    "edit_file": "editing",
    "write_file": "writing",
    "code_explore": "exploring",
}


@dataclass
class ActiveOp:
    """Tracked state for an in-flight operation."""
    op_id: str
    short_id: str
    phase: str = "CLASSIFY"
    progress_pct: float = 0.0
    provider: str = ""
    target_file: str = ""
    goal: str = ""
    risk_tier: str = ""
    started_at: float = field(default_factory=time.time)
    tool_count: int = 0
    l2_iter: int = 0
    l2_max: int = 0


@dataclass
class TriageStats:
    """Aggregate triage decision counts."""
    total: int = 0
    proceed: int = 0
    no_op: int = 0
    redirect: int = 0
    enrich: int = 0
    skip: int = 0


@dataclass
class OrganismStats:
    """Intelligence subsystem stats shown in the right panel."""
    triage: TriageStats = field(default_factory=TriageStats)
    intent_discovery_intents: int = 0
    intent_discovery_cycles: int = 0
    dream_blueprints: int = 0
    learning_rules: int = 0
    learning_trend: str = "—"  # ↑ ↓ →
    self_evo_ops: int = 0
    self_evo_success_rate: float = 0.0
    sensors_active: int = 0


# ══════════════════════════════════════════════════════════════
# LiveDashboard
# ══════════════════════════════════════════════════════════════


class LiveDashboard:
    """Persistent Rich Live dashboard for the Ouroboros battle test.

    Call `start()` to begin rendering, then update state via the public
    methods. The dashboard auto-refreshes at ~4 Hz.
    """

    def __init__(
        self,
        session_id: str = "",
        branch_name: str = "",
        cost_cap_usd: float = 0.50,
        idle_timeout_s: float = 600.0,
        repo_path: Optional[Path] = None,
    ) -> None:
        self._session_id = session_id
        self._branch_name = branch_name
        self._cost_cap = cost_cap_usd
        self._idle_timeout_s = idle_timeout_s
        self._repo_path = repo_path or Path.cwd()
        self._started_at = time.time()

        # State
        self._active_ops: Dict[str, ActiveOp] = {}
        self._completed_count: int = 0
        self._failed_count: int = 0
        self._cost_total: float = 0.0
        self._cost_breakdown: Dict[str, float] = {}
        self._cost_remaining: float = cost_cap_usd
        self._organism = OrganismStats()
        self._events: deque = deque(maxlen=30)
        self._stop_reason: str = ""
        self._rendered_preamble_keys: set[tuple[str, int]] = set()

        # Display toggles
        self._show_diffs = True
        self._expand_mode = False

        # Rich Live
        self._console = Console(emoji=True, highlight=False)
        self._live: Optional[Live] = None
        self._refresh_task: Optional[asyncio.Task] = None
        # Terminal muting state (restored on stop)
        self._muted_handlers: List[tuple] = []  # (logger, handler) pairs
        self._original_stdout: Any = None
        self._original_stderr: Any = None
        self._original_showwarning: Any = None

    @property
    def console(self) -> Console:
        return self._console

    # ── Lifecycle ─────────────────────────────────────────────

    def _mute_terminal_output(self) -> None:
        """Silence ALL terminal output that would corrupt Rich Live rendering.

        Rich Live tracks cursor position via ANSI escapes.  Any raw write
        to stdout/stderr between refreshes breaks cursor tracking and
        causes the dashboard to re-render as stacked frames.

        We silence three output channels:
        1. logging.StreamHandler on root AND all named loggers
        2. Python warnings module (warnings.warn → stderr)
        3. sys.stdout / sys.stderr (print statements, tracebacks)

        Rich Live's own Console retains a reference to the original file
        descriptor, so its rendering is unaffected by the redirect.
        All state is restored in _unmute_terminal_output().
        """
        # 1. Mute StreamHandlers on ALL loggers (root + named)
        terminal_streams = (sys.stderr, sys.stdout)
        for name in [None] + list(logging.Logger.manager.loggerDict):
            lgr = logging.getLogger(name)
            for handler in lgr.handlers[:]:
                if isinstance(handler, logging.StreamHandler) and handler.stream in terminal_streams:
                    lgr.removeHandler(handler)
                    self._muted_handlers.append((lgr, handler))

        # 2. Suppress Python warnings (FutureWarning, DeprecationWarning, etc.)
        self._original_showwarning = warnings.showwarning
        warnings.showwarning = lambda *_a, **_kw: None

        # 3. Redirect stdout/stderr to devnull so print() and tracebacks
        #    don't corrupt the dashboard.  Rich Live's Console captured the
        #    original fd at construction time, so its output is unaffected.
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        _devnull = open(os.devnull, "w")
        sys.stdout = _devnull
        sys.stderr = _devnull

    def _unmute_terminal_output(self) -> None:
        """Restore all terminal output channels silenced by _mute_terminal_output."""
        # Restore stdout/stderr
        if self._original_stdout is not None:
            sys.stdout = self._original_stdout
            self._original_stdout = None
        if self._original_stderr is not None:
            sys.stderr = self._original_stderr
            self._original_stderr = None

        # Restore warnings
        if self._original_showwarning is not None:
            warnings.showwarning = self._original_showwarning
            self._original_showwarning = None

        # Restore logging handlers
        for lgr, handler in self._muted_handlers:
            lgr.addHandler(handler)
        self._muted_handlers.clear()

    async def start(self) -> None:
        """Start the Live dashboard rendering."""
        self._started_at = time.time()
        # Mute terminal logging — raw stderr writes corrupt Rich Live's
        # cursor tracking and cause stacked frame rendering.
        self._mute_terminal_output()
        self._live = Live(
            self._build_layout(),
            console=self._console,
            refresh_per_second=4,
            screen=False,
            transient=False,
        )
        self._live.start()
        self._refresh_task = asyncio.create_task(
            self._auto_refresh(), name="dashboard_refresh",
        )

    async def stop(self) -> None:
        """Stop the Live dashboard and restore terminal logging."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        if self._live:
            try:
                self._live.stop()
            except Exception:
                pass
        self._unmute_terminal_output()

    async def _auto_refresh(self) -> None:
        """Background task to update the dashboard layout."""
        while True:
            try:
                if self._live:
                    self._live.update(self._build_layout())
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)

    # ── Public state update API ───────────────────────────────

    def add_event(self, icon: str, text: str) -> None:
        """Add an event to the scrolling log."""
        ts = time.strftime("%H:%M:%S")
        self._events.appendleft(f"[dim]{ts}[/dim] {icon} {text}")
        self._refresh()

    def op_started(
        self, op_id: str, goal: str, target_files: List[str], risk_tier: str,
    ) -> None:
        """Track a new operation."""
        short = op_id.split("-")[1][:6] if "-" in op_id else op_id[:6]
        target = target_files[0] if target_files else ""
        # Shorten target path for display
        if "/" in target and len(target) > 35:
            parts = target.split("/")
            target = "/".join(parts[-2:])
        self._active_ops[op_id] = ActiveOp(
            op_id=op_id, short_id=short, goal=goal[:60],
            target_file=target, risk_tier=risk_tier,
        )
        files_str = f"{len(target_files)} file{'s' if len(target_files) != 1 else ''}"
        self.add_event(
            "🐍",
            f"[bold]NEW[/bold]  op:{short}  {goal[:50]}  ({files_str})"
        )

    def op_phase(self, op_id: str, phase: str, progress_pct: float = 0.0) -> None:
        """Update an operation's phase."""
        op = self._active_ops.get(op_id)
        if op:
            op.phase = phase.upper()
            op.progress_pct = progress_pct
            self._refresh()

    def op_provider(self, op_id: str, provider: str, tool_count: int = 0) -> None:
        """Set the provider for an operation."""
        op = self._active_ops.get(op_id)
        if op:
            op.provider = _PROVIDER_SHORT.get(provider, provider[:10])
            op.tool_count = tool_count

    def op_tool_start(
        self,
        op_id: str,
        tool_name: str,
        args_summary: str = "",
        round_index: int = 0,
        preamble: str = "",
    ) -> None:
        """Record a per-tool-round heartbeat before the tool completes."""
        op = self._active_ops.get(op_id)
        short = op.short_id if op else op_id[:6]
        icon = _TOOL_ICONS.get(tool_name, "🔧")
        verb = _TOOL_START_VERBS.get(tool_name, tool_name.replace("_", " "))
        summary = " ".join((args_summary or "").split())[:60]

        if op:
            op.phase = "GENERATE"

        if preamble:
            key = (op_id, round_index)
            if key not in self._rendered_preamble_keys:
                self._rendered_preamble_keys.add(key)
                if len(self._rendered_preamble_keys) > 512:
                    victims = list(self._rendered_preamble_keys)[:256]
                    for victim in victims:
                        self._rendered_preamble_keys.discard(victim)
                self.add_event("🗣", f"[dim]{preamble[:100]}[/dim]  op:{short}")

        detail = f"  [dim]{summary}[/dim]" if summary else ""
        self.add_event(
            icon,
            f"[cyan]T{round_index + 1}[/cyan] {verb}{detail}  op:{short}",
        )

    def op_tool_call(
        self, op_id: str, tool_name: str, args_summary: str = "",
        round_index: int = 0, result_preview: str = "",
        duration_ms: float = 0.0, status: str = "success",
    ) -> None:
        """Record a Venom tool call."""
        op = self._active_ops.get(op_id)
        short = op.short_id if op else op_id[:6]
        if op:
            op.tool_count += 1

        icon = _TOOL_ICONS.get(tool_name, "🔧")

        dur_str = ""
        if duration_ms > 0:
            dur_str = f" ({duration_ms:.0f}ms)" if duration_ms < 1000 else f" ({duration_ms/1000:.1f}s)"

        # Show compact completion entries for expanded mode, significant tools,
        # or anything that did not succeed cleanly.
        if self._expand_mode or tool_name in ("run_tests", "bash") or status != "success":
            self.add_event(
                icon,
                f"[cyan]T{round_index+1}[/cyan] {tool_name}"
                f"  [dim]{args_summary[:40]}[/dim]{dur_str}  op:{short}",
            )

    def op_l2_repair(self, op_id: str, iteration: int, max_iters: int, status: str) -> None:
        """Track L2 repair iteration."""
        op = self._active_ops.get(op_id)
        short = op.short_id if op else op_id[:6]
        if op:
            op.l2_iter = iteration
            op.l2_max = max_iters
        color = "green" if status == "converged" else "yellow" if status != "failed" else "red"
        self.add_event(
            "🔧",
            f"[{color}]L2 [{iteration}/{max_iters}] {status}[/{color}]  op:{short}",
        )

    def op_validation(self, op_id: str, passed: bool, test_count: int = 0, failures: int = 0) -> None:
        """Record validation result."""
        op = self._active_ops.get(op_id)
        short = op.short_id if op else op_id[:6]
        if passed:
            self.add_event("🧪", f"[green]{test_count} tests passed ✓[/green]  op:{short}")
        else:
            self.add_event("🧪", f"[red]{failures}/{test_count} failed ✗[/red]  op:{short}")

    def op_generation(
        self, op_id: str, candidates: int, provider: str,
        duration_s: float = 0.0, tool_count: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Record generation result."""
        op = self._active_ops.get(op_id)
        short = op.short_id if op else op_id[:6]
        prov = _PROVIDER_SHORT.get(provider, provider[:10])
        if op:
            op.provider = prov
            op.tool_count = tool_count
        tools_str = f" + {tool_count} tools" if tool_count > 0 else ""
        if cost_usd >= 0.01:
            cost_str = f"  ${cost_usd:.2f}"
        elif cost_usd > 0.001:
            cost_str = f"  ${cost_usd:.3f}"
        else:
            cost_str = ""
        self.add_event(
            "✨",
            f"[bold]GENERATE[/bold] {candidates} candidate(s) via {prov}"
            f"{tools_str}{cost_str}  [dim]({duration_s:.1f}s)[/dim]  op:{short}",
        )

    # ── Code generation & diff display (rendered above dashboard) ──

    def _print_above(self, renderable: Any) -> None:
        """Print a Rich renderable above the pinned dashboard.

        Rich Live with screen=False keeps the dashboard at the bottom;
        console.print() output scrolls above it — giving the user both
        a persistent overview AND detailed code/diff output.
        """
        if self._live and self._live.console:
            self._live.console.print(renderable)
        else:
            self._console.print(renderable)

    def show_code_preview(
        self,
        op_id: str,
        provider: str,
        candidate_files: List[str],
        candidate_preview: str = "",
        duration_s: float = 0.0,
        tool_count: int = 0,
    ) -> None:
        """Show syntax-highlighted code preview above the dashboard.

        Called when generation completes — shows the actual code that
        Ouroboros wrote so the user can see it in real time.
        """
        op = self._active_ops.get(op_id)
        short = op.short_id if op else op_id[:6]
        prov = _PROVIDER_SHORT.get(provider, provider[:10])
        tools_str = f" + {tool_count} Venom tools" if tool_count > 0 else ""

        # Header line
        self._print_above(Text.from_markup(
            f"\n  ✨ [bold]GENERATED[/bold]  op:{short}  via [cyan]{prov}[/cyan]"
            f"{tools_str}  [dim]({duration_s:.1f}s)[/dim]"
        ))

        # File list
        if candidate_files:
            for f in candidate_files[:8]:
                self._print_above(Text.from_markup(f"     📄 [cyan]{f}[/cyan]"))

        # Syntax-highlighted code preview
        if candidate_preview:
            preview = candidate_preview
            truncated = False
            if not self._expand_mode and len(preview) > 1500:
                preview = preview[:1500]
                truncated = True

            # Detect language from file extension
            lang = "python"
            if candidate_files:
                ext = candidate_files[0].rsplit(".", 1)[-1] if "." in candidate_files[0] else ""
                lang_map = {"py": "python", "ts": "typescript", "js": "javascript",
                            "json": "json", "yaml": "yaml", "yml": "yaml", "md": "markdown"}
                lang = lang_map.get(ext, "python")

            try:
                syntax = Syntax(
                    preview, lang, theme="monokai",
                    line_numbers=True, word_wrap=True,
                )
                subtitle = f"[dim]+{len(candidate_preview) - 1500} chars truncated[/dim]" if truncated else ""
                self._print_above(Panel(
                    syntax,
                    title=f"[bold green]✨ Generated Code[/bold green]  op:{short}",
                    subtitle=subtitle,
                    border_style="green",
                    padding=(0, 1),
                ))
            except Exception:
                self._print_above(Text.from_markup(f"  [dim]{preview[:500]}[/dim]"))

        self._print_above(Text(""))  # spacing

    def show_diff(
        self,
        file_path: str,
        diff_text: str = "",
        op_id: str = "",
    ) -> None:
        """Show colored git diff above the dashboard.

        Called during APPLY phase — shows the actual changes being
        written to disk so the user can review in real time.
        """
        if not self._show_diffs:
            return

        short = ""
        if op_id:
            op = self._active_ops.get(op_id)
            short = op.short_id if op else op_id[:6]

        # Get diff from git if not provided
        if not diff_text:
            diff_text = self._get_git_diff(file_path)

        if not diff_text:
            self._print_above(Text.from_markup(
                f"  💾 [green]APPLY:[/green] {file_path}"
                + (f"  [dim]op:{short}[/dim]" if short else "")
            ))
            return

        # Truncate very large diffs
        truncated = False
        if not self._expand_mode and len(diff_text) > 3000:
            lines = diff_text.split("\n")
            diff_text = "\n".join(lines[:80])
            truncated = True

        try:
            syntax = Syntax(
                diff_text, "diff", theme="monokai",
                line_numbers=True, word_wrap=True,
            )
            subtitle_parts = []
            if short:
                subtitle_parts.append(f"op:{short}")
            if truncated:
                subtitle_parts.append("truncated — press 'e' to expand")
            subtitle = "  ".join(subtitle_parts) if subtitle_parts else None

            self._print_above(Panel(
                syntax,
                title=f"[bold]📝 {file_path}[/bold]",
                subtitle=f"[dim]{subtitle}[/dim]" if subtitle else None,
                border_style="green",
                padding=(0, 1),
            ))
        except Exception:
            # Fallback: raw colored lines
            self._print_above(Text.from_markup(
                f"  💾 [green]APPLY:[/green] {file_path}"
            ))

    def show_completion_panel(
        self,
        op_id: str,
        files_changed: List[str],
        provider: str,
        duration_s: float,
        cost_usd: float = 0.0,
    ) -> None:
        """Show a success/signature panel above the dashboard."""
        short = op_id.split("-")[1][:6] if "-" in op_id else op_id[:6]
        prov = _PROVIDER_SHORT.get(provider, provider[:10])
        cost_str = f"  💰 ${cost_usd:.4f}" if cost_usd > 0 else ""

        self._print_above(Panel(
            Text.from_markup(
                f"Generated-By: [bold]Ouroboros + Venom + Consciousness[/bold]\n"
                f"Signed-off-by: [dim]JARVIS Ouroboros <ouroboros@jarvis.local>[/dim]\n"
                f"Provider: [cyan]{prov}[/cyan]  "
                f"Files: {', '.join(f[:30] for f in files_changed[:3])}"
            ),
            title=f"[bold green]✅ SUCCESS[/bold green]  ⏱ {duration_s:.1f}s{cost_str}",
            subtitle=f"[dim]op:{short}[/dim]",
            border_style="green",
            padding=(0, 2),
        ))

    def show_failure_panel(
        self,
        op_id: str,
        reason: str,
        phase: str = "",
        duration_s: float = 0.0,
    ) -> None:
        """Show a failure panel above the dashboard."""
        short = op_id.split("-")[1][:6] if "-" in op_id else op_id[:6]
        phase_str = f" at [bold]{phase}[/bold]" if phase else ""

        self._print_above(Panel(
            Text.from_markup(
                f"[red]Reason: {reason}[/red]{phase_str}\n"
                f"[dim]The organism will learn from this failure.[/dim]"
            ),
            title=f"[bold red]❌ FAILED[/bold red]  ⏱ {duration_s:.1f}s",
            subtitle=f"[dim]op:{short}[/dim]",
            border_style="red",
            padding=(0, 2),
        ))

    def _get_git_diff(self, file_path: str) -> str:
        """Get git diff for a file."""
        for args in (
            ["git", "diff", "--cached", "--", file_path],
            ["git", "diff", "--", file_path],
            ["git", "diff", "HEAD~1", "--", file_path],
        ):
            try:
                result = subprocess.run(
                    args, cwd=self._repo_path,
                    capture_output=True, text=True, timeout=5,
                )
                if result.stdout.strip():
                    return result.stdout.strip()
            except Exception:
                continue
        return ""

    # ── Streaming output ──────────────────────────────────────

    def show_streaming_start(self, provider: str, op_id: str = "") -> None:
        """Begin streaming code generation output above the dashboard."""
        prov = _PROVIDER_SHORT.get(provider, provider[:10])
        short = op_id.split("-")[1][:6] if "-" in op_id else op_id[:6] if op_id else ""
        id_str = f"  op:{short}" if short else ""
        self._print_above(Text.from_markup(
            f"\n  ✨ [dim]Generating via [cyan]{prov}[/cyan]...{id_str}[/dim]"
        ))
        # Start dim text for streaming tokens
        sys.stdout.write("  \033[2m")
        sys.stdout.flush()

    def show_streaming_token(self, token: str) -> None:
        """Print a streaming token chunk (real-time code writing)."""
        sys.stdout.write(token)
        sys.stdout.flush()

    def show_streaming_end(self) -> None:
        """End the streaming output block."""
        sys.stdout.write("\033[0m\n")
        sys.stdout.flush()

    # ── Operation completion (updates dashboard + prints panels) ──

    def op_completed(
        self, op_id: str, files_changed: List[str],
        provider: str = "", cost_usd: float = 0.0,
    ) -> None:
        """Mark an operation as complete and show success panel."""
        op = self._active_ops.pop(op_id, None)
        short = op.short_id if op else op_id[:6]
        self._completed_count += 1
        elapsed = time.time() - (op.started_at if op else time.time())
        files_str = f"{len(files_changed)} file{'s' if len(files_changed) != 1 else ''}"
        cost_str = f" ${cost_usd:.4f}" if cost_usd > 0 else ""

        # Show diffs above dashboard
        if self._show_diffs and files_changed:
            for f in files_changed[:5]:
                self.show_diff(f, op_id=op_id)

        # Show completion panel above dashboard
        self.show_completion_panel(
            op_id=op_id, files_changed=files_changed,
            provider=provider, duration_s=elapsed, cost_usd=cost_usd,
        )

        # Update event log
        self.add_event(
            "✅",
            f"[bold green]COMPLETE[/bold green]  op:{short}  "
            f"{files_str} changed  [dim]{elapsed:.0f}s{cost_str}[/dim]",
        )

    def op_failed(self, op_id: str, reason: str, phase: str = "") -> None:
        """Mark an operation as failed and show failure panel."""
        op = self._active_ops.pop(op_id, None)
        short = op.short_id if op else op_id[:6]
        self._failed_count += 1
        elapsed = time.time() - (op.started_at if op else time.time())
        phase_str = f" at {phase}" if phase else ""

        # Show failure panel above dashboard
        self.show_failure_panel(
            op_id=op_id, reason=reason, phase=phase, duration_s=elapsed,
        )

        self.add_event(
            "❌",
            f"[bold red]FAILED[/bold red]  op:{short}  {reason[:50]}{phase_str}",
        )

    def op_noop(self, op_id: str, reason: str = "") -> None:
        """Record a triage NO_OP (operation skipped before generation)."""
        op = self._active_ops.pop(op_id, None)
        short = op.short_id if op else op_id[:6]
        self._organism.triage.no_op += 1
        self._organism.triage.total += 1
        self.add_event(
            "⏭️",
            f"[dim]NO_OP  op:{short}  {reason[:50]}[/dim]",
        )

    # ── Organism intelligence updates ─────────────────────────

    def update_triage(self, decision: str, op_id: str = "", confidence: float = 0.0) -> None:
        """Record a semantic triage decision."""
        self._organism.triage.total += 1
        d = decision.upper()
        if d == "PROCEED":
            self._organism.triage.proceed += 1
        elif d == "NO_OP":
            self._organism.triage.no_op += 1
        elif d == "REDIRECT":
            self._organism.triage.redirect += 1
        elif d == "ENRICH":
            self._organism.triage.enrich += 1
        elif d == "SKIP":
            self._organism.triage.skip += 1
        short = op_id.split("-")[1][:6] if "-" in op_id else op_id[:6] if op_id else "—"
        color = {"PROCEED": "green", "NO_OP": "dim", "REDIRECT": "cyan",
                 "ENRICH": "yellow", "SKIP": "dim"}.get(d, "white")
        self.add_event(
            "🧠",
            f"TRIAGE [{color}]{d}[/{color}]"
            f"  [dim]({confidence:.0%})[/dim]  op:{short}",
        )

    def update_intent_discovery(self, cycle: int, submitted: int) -> None:
        """Record an IntentDiscovery sensor cycle."""
        self._organism.intent_discovery_cycles = cycle
        self._organism.intent_discovery_intents += submitted
        self.add_event(
            "🧬",
            f"[magenta]DISCOVERY[/magenta] cycle {cycle}  "
            f"{submitted} intent{'s' if submitted != 1 else ''} submitted",
        )

    def update_dream_engine(self, blueprints: int, title: str = "") -> None:
        """Record DreamEngine activity."""
        self._organism.dream_blueprints = blueprints
        if title:
            self.add_event("💭", f"DREAM  \"{title[:40]}\"  ({blueprints} total)")

    def update_learning(self, rules: int, trend: str = "→") -> None:
        """Update learning consolidation stats."""
        self._organism.learning_rules = rules
        self._organism.learning_trend = trend
        if rules > 0:
            self.add_event("📖", f"LEARNING  {rules} rules consolidated  trend: {trend}")

    def update_self_evo(self, total_ops: int, success_rate: float) -> None:
        """Update self-evolution tracking."""
        self._organism.self_evo_ops = total_ops
        self._organism.self_evo_success_rate = success_rate

    def update_sensors(self, active_count: int) -> None:
        """Update sensor count."""
        self._organism.sensors_active = active_count

    def update_cost(self, total: float, remaining: float, breakdown: Dict[str, float]) -> None:
        """Update cost tracking."""
        self._cost_total = total
        self._cost_remaining = remaining
        self._cost_breakdown = breakdown
        self._refresh()

    # ── Toggle controls ───────────────────────────────────────

    def toggle_expand(self) -> None:
        self._expand_mode = not self._expand_mode

    def toggle_diffs(self) -> None:
        self._show_diffs = not self._show_diffs

    # ── Layout building ───────────────────────────────────────

    def _refresh(self) -> None:
        """Force a layout refresh."""
        if self._live:
            try:
                self._live.update(self._build_layout())
            except Exception:
                pass

    def _build_layout(self) -> Panel:
        """Build the complete dashboard layout."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=4),
            Layout(name="body", ratio=1),
            Layout(name="log", size=14),
            Layout(name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="ops", ratio=3),
            Layout(name="intel", ratio=2),
        )

        layout["header"].update(self._build_header())
        layout["ops"].update(self._build_ops_panel())
        layout["intel"].update(self._build_intel_panel())
        layout["log"].update(self._build_log_panel())
        layout["footer"].update(self._build_footer())

        return Panel(
            layout,
            title="[bold cyan]🐍 OUROBOROS + VENOM[/bold cyan]",
            border_style="cyan",
            padding=0,
        )

    def _build_header(self) -> Panel:
        """Build the session header with budget bar."""
        elapsed = time.time() - self._started_at
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)

        # Budget progress bar
        spent_pct = min(1.0, self._cost_total / max(0.01, self._cost_cap))
        budget_color = "green" if spent_pct < 0.6 else "yellow" if spent_pct < 0.85 else "red"

        bar_width = 20
        filled = int(spent_pct * bar_width)
        bar = f"[{budget_color}]{'█' * filled}[/{budget_color}]" + f"[dim]{'░' * (bar_width - filled)}[/dim]"

        ops_str = (
            f"[green]{self._completed_count} ✓[/green]"
            f"  [red]{self._failed_count} ✗[/red]"
            f"  [cyan]{len(self._active_ops)} ⟳[/cyan]"
        )

        header = Text.from_markup(
            f"  Session [bold]{self._session_id}[/bold]"
            f"  │  Branch [cyan]{self._branch_name or 'N/A'}[/cyan]"
            f"  │  ⏱ {mins}m {secs:02d}s\n"
            f"  Budget [{bar}] "
            f"[{budget_color}]${self._cost_total:.3f}[/{budget_color}]"
            f"/${self._cost_cap:.2f}"
            f"  │  Ops: {ops_str}"
        )
        return Panel(header, style="dim", padding=(0, 0))

    def _build_ops_panel(self) -> Panel:
        """Build the active operations table."""
        table = Table(
            show_header=True, header_style="bold cyan",
            box=None, padding=(0, 1), expand=True,
        )
        table.add_column("ID", width=7, no_wrap=True)
        table.add_column("Phase", width=12, no_wrap=True)
        table.add_column("Prov", width=8, no_wrap=True)
        table.add_column("Target", ratio=1, no_wrap=True)
        table.add_column("⏱", width=6, justify="right", no_wrap=True)
        table.add_column("🔧", width=3, justify="right", no_wrap=True)

        if not self._active_ops:
            table.add_row(
                "", "[dim]No active operations — sensors scanning...[/dim]",
                "", "", "", "",
            )
        else:
            for op in sorted(
                self._active_ops.values(),
                key=lambda o: o.started_at,
            ):
                elapsed = time.time() - op.started_at
                dur = f"{elapsed:.0f}s" if elapsed < 60 else f"{elapsed/60:.1f}m"
                phase_lower = op.phase.lower()
                icon = _PHASE_ICONS.get(phase_lower, "▶")
                color = _PHASE_COLORS.get(phase_lower, "white")

                # Phase display with L2 indicator
                phase_display = f"{icon} [{color}]{op.phase}[/{color}]"
                if op.l2_iter > 0:
                    phase_display += f" [yellow]L2:{op.l2_iter}/{op.l2_max}[/yellow]"

                risk_badge = ""
                if op.risk_tier:
                    r = op.risk_tier.upper()
                    if r in ("SAFE_AUTO", "LOW"):
                        risk_badge = "[green]●[/green]"
                    elif r in ("MEDIUM",):
                        risk_badge = "[yellow]●[/yellow]"
                    else:
                        risk_badge = "[red]●[/red]"

                tools = str(op.tool_count) if op.tool_count > 0 else ""

                table.add_row(
                    f"{risk_badge}{op.short_id}",
                    phase_display,
                    op.provider or "[dim]—[/dim]",
                    f"[dim]{op.target_file}[/dim]" if op.target_file else "[dim]—[/dim]",
                    f"[dim]{dur}[/dim]",
                    f"[dim]{tools}[/dim]" if tools else "",
                )

        return Panel(
            table,
            title=f"[bold]Active Operations ({len(self._active_ops)})[/bold]",
            border_style="cyan",
            padding=(0, 0),
        )

    def _build_intel_panel(self) -> Panel:
        """Build the organism intelligence panel."""
        org = self._organism
        lines: List[str] = []

        # Triage stats
        t = org.triage
        if t.total > 0:
            lines.append(f"[bold]🧠 Triage[/bold]  {t.total} screened")
            parts = []
            if t.proceed: parts.append(f"[green]{t.proceed} PROCEED[/green]")
            if t.no_op: parts.append(f"[dim]{t.no_op} NO_OP[/dim]")
            if t.redirect: parts.append(f"[cyan]{t.redirect} REDIRECT[/cyan]")
            if t.enrich: parts.append(f"[yellow]{t.enrich} ENRICH[/yellow]")
            if t.skip: parts.append(f"[dim]{t.skip} SKIP[/dim]")
            lines.append(f"   {', '.join(parts)}")
            if t.total > 0:
                save_pct = (t.no_op + t.skip) / t.total
                if save_pct > 0:
                    lines.append(f"   [dim]💰 {save_pct:.0%} budget saved[/dim]")
        else:
            lines.append("[dim]🧠 Triage  waiting for ops...[/dim]")

        lines.append("")

        # Intent Discovery
        if org.intent_discovery_cycles > 0:
            lines.append(
                f"[bold]🧬 Discovery[/bold]  {org.intent_discovery_intents} intents"
                f"  [dim]({org.intent_discovery_cycles} cycles)[/dim]"
            )
        else:
            lines.append("[dim]🧬 Discovery  booting (30s delay)...[/dim]")

        # DreamEngine
        if org.dream_blueprints > 0:
            lines.append(f"[bold]💭 Dreams[/bold]  {org.dream_blueprints} blueprints")
        else:
            lines.append("[dim]💭 Dreams  waiting for idle...[/dim]")

        lines.append("")

        # Learning
        if org.learning_rules > 0:
            lines.append(
                f"[bold]📖 Learning[/bold]  {org.learning_rules} rules"
                f"  trend: {org.learning_trend}"
            )
        else:
            lines.append("[dim]📖 Learning  accumulating outcomes...[/dim]")

        # Self-Evolution
        if org.self_evo_ops > 0:
            pct = org.self_evo_success_rate
            color = "green" if pct >= 0.7 else "yellow" if pct >= 0.4 else "red"
            lines.append(
                f"[bold]🧬 Self-Evo[/bold]  {org.self_evo_ops} ops"
                f"  [{color}]{pct:.0%} success[/{color}]"
            )

        # Sensors
        if org.sensors_active > 0:
            lines.append(f"\n[dim]📡 {org.sensors_active} sensors active[/dim]")

        content = Text.from_markup("\n".join(lines))
        return Panel(
            content,
            title="[bold]Organism Intelligence[/bold]",
            border_style="magenta",
            padding=(0, 1),
        )

    def _build_log_panel(self) -> Panel:
        """Build the scrolling event log."""
        if not self._events:
            content = Text.from_markup("[dim]  Waiting for events...[/dim]")
        else:
            lines = list(self._events)[:12]  # Show last 12
            content = Text.from_markup("\n".join(f"  {ln}" for ln in lines))

        return Panel(
            content,
            title=f"[bold]Event Log[/bold] [dim]({len(self._events)} events)[/dim]",
            border_style="dim",
            padding=(0, 0),
        )

    def _build_footer(self) -> Panel:
        """Build the footer with cost breakdown and controls."""
        # Cost breakdown
        cost_parts = []
        for provider, cost in sorted(self._cost_breakdown.items()):
            if cost > 0:
                cost_parts.append(f"{provider}: ${cost:.4f}")
        cost_str = "  ".join(cost_parts) if cost_parts else "no spend yet"

        controls = (
            "[Ctrl+C: stop]  [e: expand]  [d: diffs]"
        )

        footer = Text.from_markup(
            f"  💰 {cost_str}    │    {controls}"
        )
        return Panel(footer, style="dim", padding=(0, 0))


# ══════════════════════════════════════════════════════════════
# DashboardTransport — CommProtocol adapter
# ══════════════════════════════════════════════════════════════


class DashboardTransport:
    """CommProtocol transport that routes messages to LiveDashboard.

    Replaces OuroborosTUITransport when the dashboard is active.
    """

    def __init__(self, dashboard: LiveDashboard) -> None:
        self._db = dashboard
        self._op_providers: Dict[str, str] = {}

    async def send(self, msg: Any) -> None:
        """Handle a CommMessage and update the dashboard."""
        try:
            payload = msg.payload if hasattr(msg, "payload") else {}
            op_id = msg.op_id if hasattr(msg, "op_id") else ""
            msg_type = msg.msg_type.value if hasattr(msg, "msg_type") else ""

            if msg_type == "INTENT":
                if payload.get("risk_tier") not in ("routing",):
                    self._db.op_started(
                        op_id=op_id,
                        goal=payload.get("goal", ""),
                        target_files=payload.get("target_files", []),
                        risk_tier=payload.get("risk_tier", ""),
                    )

            elif msg_type == "HEARTBEAT":
                phase = payload.get("phase", "")

                # Triage decision
                if phase == "semantic_triage" and payload.get("triage_decision"):
                    self._db.update_triage(
                        decision=payload["triage_decision"],
                        op_id=op_id,
                        confidence=payload.get("triage_confidence", 0.0),
                    )
                    if payload["triage_decision"].upper() == "NO_OP":
                        self._db.op_noop(op_id, payload.get("triage_reason", ""))

                # Tool call
                elif payload.get("tool_name"):
                    if payload.get("tool_starting"):
                        self._db.op_tool_start(
                            op_id=op_id,
                            tool_name=payload["tool_name"],
                            args_summary=payload.get("tool_args_summary", ""),
                            round_index=payload.get("round_index", 0),
                            preamble=payload.get("preamble", ""),
                        )
                    else:
                        self._db.op_tool_call(
                            op_id=op_id,
                            tool_name=payload["tool_name"],
                            args_summary=payload.get("tool_args_summary", ""),
                            round_index=payload.get("round_index", 0),
                            result_preview=payload.get("result_preview", ""),
                            duration_ms=payload.get("duration_ms", 0.0),
                            status=payload.get("status", "success"),
                        )

                # Generation result
                elif payload.get("candidates_count") is not None:
                    provider = payload.get("provider", "unknown")
                    self._op_providers[op_id] = provider
                    self._db.op_generation(
                        op_id=op_id,
                        candidates=payload["candidates_count"],
                        provider=provider,
                        duration_s=payload.get("generation_duration_s", 0.0),
                        tool_count=payload.get("tool_records", 0),
                        cost_usd=payload.get("cost_usd", 0.0),
                    )
                    # Show code preview above dashboard when candidate files present
                    candidate_files = payload.get("candidate_files", [])
                    if candidate_files or payload.get("candidate_preview"):
                        self._db.show_code_preview(
                            op_id=op_id,
                            provider=provider,
                            candidate_files=candidate_files,
                            candidate_preview=payload.get("candidate_preview", ""),
                            duration_s=payload.get("generation_duration_s", 0.0),
                            tool_count=payload.get("tool_records", 0),
                        )

                # Validation
                elif phase.upper() in ("VALIDATE", "VALIDATE_RETRY") and "test_passed" in payload:
                    self._db.op_validation(
                        op_id=op_id,
                        passed=payload.get("test_passed", False),
                        test_count=payload.get("test_count", 0),
                        failures=payload.get("test_failures", 0),
                    )

                # L2 repair
                elif payload.get("l2_iteration") is not None:
                    self._db.op_l2_repair(
                        op_id=op_id,
                        iteration=payload["l2_iteration"],
                        max_iters=payload.get("l2_max_iters", 5),
                        status=payload.get("l2_status", ""),
                    )

                # APPLY phase — show real-time diffs
                elif phase.upper() == "APPLY" and payload.get("target_file"):
                    self._db.show_diff(
                        file_path=payload["target_file"],
                        diff_text=payload.get("diff_text", ""),
                        op_id=op_id,
                    )

                # Streaming code generation tokens
                elif payload.get("streaming") == "start":
                    provider = payload.get("provider", "unknown")
                    self._op_providers[op_id] = provider
                    self._db.show_streaming_start(provider=provider, op_id=op_id)
                elif payload.get("streaming") == "token":
                    self._db.show_streaming_token(payload.get("token", ""))
                elif payload.get("streaming") == "end":
                    self._db.show_streaming_end()

                # IntentDiscovery sensor
                elif payload.get("intent_discovery_cycle") is not None:
                    self._db.update_intent_discovery(
                        cycle=payload["intent_discovery_cycle"],
                        submitted=payload.get("intent_discovery_submitted", 0),
                    )

                # DreamEngine
                elif payload.get("dream_blueprints") is not None:
                    self._db.update_dream_engine(
                        blueprints=payload["dream_blueprints"],
                        title=payload.get("dream_title", ""),
                    )

                # Standard phase transition
                elif phase and ":" not in phase:
                    self._db.op_phase(
                        op_id=op_id,
                        phase=phase,
                        progress_pct=payload.get("progress_pct", 0.0),
                    )

            elif msg_type == "DECISION":
                outcome = payload.get("outcome", "")
                files = payload.get("files_changed", payload.get("affected_files", []))
                provider = self._op_providers.pop(op_id, "unknown")

                if outcome in ("completed", "applied", "auto_approved"):
                    self._db.op_completed(
                        op_id=op_id,
                        files_changed=files,
                        provider=provider,
                        cost_usd=payload.get("cost_usd", 0.0),
                    )
                elif outcome in ("failed", "postmortem"):
                    self._db.op_failed(
                        op_id=op_id,
                        reason=payload.get("reason_code", outcome),
                        phase=payload.get("failed_phase", ""),
                    )

            elif msg_type == "POSTMORTEM":
                self._db.op_failed(
                    op_id=op_id,
                    reason=payload.get("root_cause", "unknown"),
                    phase=payload.get("failed_phase", ""),
                )

        except Exception:
            pass  # Dashboard should never crash the pipeline


# ══════════════════════════════════════════════════════════════
# DashboardKeyboardHandler
# ══════════════════════════════════════════════════════════════


class DashboardKeyboardHandler:
    """Non-blocking keyboard input for the dashboard.

    e: Toggle expand mode (show all tool calls in log)
    d: Toggle diff file paths in completion events
    """

    def __init__(
        self,
        dashboard: LiveDashboard,
        shutdown_event: Optional[asyncio.Event] = None,
    ) -> None:
        self._db = dashboard
        self._shutdown_event = shutdown_event
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._old_settings: Any = None

    async def start(self) -> None:
        if sys.stdin.isatty():
            self._running = True
            self._task = asyncio.create_task(
                self._input_loop(), name="dashboard_keyboard",
            )

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _input_loop(self) -> None:
        import termios
        import tty
        loop = asyncio.get_running_loop()
        try:
            self._old_settings = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
            while self._running:
                try:
                    char = await asyncio.wait_for(
                        loop.run_in_executor(None, sys.stdin.read, 1),
                        timeout=0.5,
                    )
                except asyncio.TimeoutError:
                    continue
                except (EOFError, OSError):
                    break
                if not char:
                    continue
                if char == "e":
                    self._db.toggle_expand()
                elif char == "d":
                    self._db.toggle_diffs()
        except Exception:
            pass
        finally:
            if self._old_settings is not None:
                try:
                    termios.tcsetattr(
                        sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings,
                    )
                except Exception:
                    pass
