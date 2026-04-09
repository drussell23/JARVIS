"""Serpent Flow — Ouroboros Flowing CLI with Organism Personality.

Layout Architecture (4-zone vertical stack):

  Zone 0: Boot Banner — compact Rich Panel, scrolls away after boot
  Zone 1: Event Stream — op-scoped blocks with box-drawing borders
  Zone 2: REPL Input — fixed bottom via prompt_toolkit.prompt_async()
  Zone 3: Status Bar — persistent toolbar via prompt_toolkit.bottom_toolbar

Op blocks use box-drawing characters for visual hierarchy::

  ┌ a7f3 ── TestFailure ──────────────────────────
  │  🔬 sensed    test_voice_pipeline
  │  🧬 synth     via DW-397B
  │  ┌─ 📄 read_file ────────────────────────────
  │  │  backend/voice/pipeline.py  38 lines  42ms
  │  └────────────────────────────────────────────
  │  ✨ evolved   1 file changed │ ⏱ 22.3s
  └ a7f3 ── 🐍 ✅ 1  💀 0 │ 💰 $0.003 ──────────

Manifesto §7: Absolute Observability — the inner workings of the
symbiote must be entirely visible.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.status import Status
from rich.syntax import Syntax

# ══════════════════════════════════════════════════════════════
# Color palette (organism theme)
# ══════════════════════════════════════════════════════════════

_C = {
    "life": "bright_green",      # awakening, success, evolved
    "neural": "cyan",            # thinking, processing, phases
    "provider": "magenta",       # external brains (DW, Claude, J-Prime)
    "file": "blue underline",    # file paths — clickable feel
    "heal": "yellow",            # repair, immune response, caution
    "death": "red",              # failure, rejection, shed
    "dim": "dim",                # metadata (IDs, costs, timestamps)
    "border": "dim",             # box-drawing borders
    "code_add": "green",         # diff: added lines
    "code_del": "red",           # diff: removed lines
    "code_hunk": "cyan",         # diff: @@ hunk headers
}

# Provider display names
_PROV = {
    "doubleword-397b": "DW-397B", "doubleword": "DW-397B",
    "claude-api": "Claude", "claude": "Claude",
    "gcp-jprime": "J-Prime",
}

# Language detection for syntax highlighting
_LANG_MAP = {
    "py": "python", "ts": "typescript", "js": "javascript",
    "json": "json", "yaml": "yaml", "yml": "yaml",
    "md": "markdown", "rs": "rust", "go": "go",
    "sh": "bash", "bash": "bash", "zsh": "bash",
    "cpp": "cpp", "c": "c", "h": "cpp",
}

# Rich markup stripping for visible-length calculation
_MARKUP_RE = re.compile(r"\[/?[^\]]*\]")


def _detect_lang(file_path: str) -> str:
    """Detect syntax language from file extension."""
    if "." in file_path:
        ext = file_path.rsplit(".", 1)[-1].lower()
        return _LANG_MAP.get(ext, "python")
    return "python"


def _short_id(op_id: str) -> str:
    """Extract a unique short display ID from an op_id.

    Op IDs use UUIDv7 format: ``op-019d6fbd-e010-7f4a-a118-7972ac22de4c-jarvis``
    The first 12 hex chars are a millisecond timestamp (shared within a session).
    We skip the ``op-`` prefix and timestamp, then take 6 chars from the random
    portion to get a unique per-operation identifier.
    """
    raw = op_id
    if raw.startswith("op-"):
        raw = raw[3:]
    hex_only = raw.replace("-", "")
    if len(hex_only) > 18:
        return hex_only[12:18]
    return hex_only[-6:] if len(hex_only) >= 6 else hex_only


def _prov(provider: str) -> str:
    """Normalize provider name for display."""
    return _PROV.get(provider, provider[:12])


def _visible_len(text: str) -> int:
    """Length of text after stripping Rich markup tags."""
    return len(_MARKUP_RE.sub("", text))


# ══════════════════════════════════════════════════════════════
# SerpentFlow — the flowing organism CLI
# ══════════════════════════════════════════════════════════════


class SerpentFlow:
    """Ouroboros flowing CLI with 4-zone layout architecture.

    Zone 0: Boot Banner — compact Rich Panel with 6-layer status
    Zone 1: Event Stream — op-scoped blocks with box-drawing borders
    Zone 2: REPL Input — fixed bottom via prompt_toolkit
    Zone 3: Status Bar — persistent toolbar with live metrics

    Parameters
    ----------
    session_id:
        Battle test session identifier.
    branch_name:
        Git branch the organism is working on.
    cost_cap_usd:
        Session budget ceiling.
    idle_timeout_s:
        Inactivity timeout.
    repo_path:
        Repository root for git diff lookups.
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

        # Tracking
        self._completed: int = 0
        self._failed: int = 0
        self._cost_total: float = 0.0
        self._sensors_active: int = 0
        self._op_providers: Dict[str, str] = {}
        self._op_starts: Dict[str, float] = {}
        self._streaming_active: bool = False

        # Op block tracking — set of op_ids with visually open blocks
        self._active_ops: set = set()
        # Sensor type per op (for close border label)
        self._op_sensors: Dict[str, str] = {}

        # Rich console
        self.console = Console(emoji=True, highlight=False)

        # Execution masking (rich.Status)
        self._active_status: Optional[Status] = None

        # Live Markdown streaming (rich.Live + rich.Markdown)
        self._live: Optional[Live] = None
        self._stream_buffer: str = ""

    # ══════════════════════════════════════════════════════════
    # Zone 0: Boot Banner
    # ══════════════════════════════════════════════════════════

    def boot_banner(
        self,
        layers: List[tuple],
        n_sensors: int = 0,
        log_path: str = "",
    ) -> None:
        """Render the compact boot banner as a Rich Panel.

        Parameters
        ----------
        layers:
            List of ``(icon, name, is_on, detail)`` tuples for the 6-layer
            organism status display.
        n_sensors:
            Number of active intake sensors.
        log_path:
            Path to the debug log file (shown in banner footer).
        """
        _on = "[bright_green]ON[/bright_green] "
        _off = "[dim]OFF[/dim]"

        lines: List[str] = []

        # Identity block
        lines.append(f"[bold]Session[/bold]    [dim]{self._session_id}[/dim]")
        lines.append(f"[bold]Branch[/bold]     [dim]{self._branch_name or 'N/A'}[/dim]")
        lines.append(
            f"[bold]Budget[/bold]     ${self._cost_cap:.2f}  [dim]│[/dim]  "
            f"Idle {self._idle_timeout_s:.0f}s"
        )
        lines.append("[bold]Mode[/bold]       Governed (SAFE_AUTO auto-apply)")
        lines.append("")

        # Layer status
        lines.append("[bold]── 6-Layer Organism ─────────────────────────[/bold]")
        for icon, name, is_on, detail in layers:
            status = _on if is_on else _off
            lines.append(f"  {icon}  {name:<24s} {status} [dim]{detail}[/dim]")

        # Footer
        lines.append("")
        sensor_str = f" [dim]│[/dim] {n_sensors} sensors" if n_sensors else ""
        lines.append(f"[bright_green]🔋 Organism alive[/bright_green]{sensor_str} [dim]│[/dim] Ctrl+C to stop")
        if log_path:
            lines.append(f"[dim]📝 {log_path}[/dim]")

        panel = Panel(
            "\n".join(lines),
            title="[bold cyan]🐍 O U R O B O R O S  +  V E N O M[/bold cyan]",
            subtitle="[bold cyan]The Self-Developing Organism[/bold cyan]",
            border_style="bold cyan",
            width=min(self.console.width, 72),
            padding=(1, 2),
        )
        self.console.print()
        self.console.print(panel)
        self.console.print()

    # ══════════════════════════════════════════════════════════
    # Lifecycle
    # ══════════════════════════════════════════════════════════

    async def start(self) -> None:
        """Print the awakening banner (minimal — boot_banner handles the heavy lifting)."""
        c = self.console
        c.print()
        c.print(
            f"  [{_C['life']}]🐍 ouroboros[/{_C['life']}] [dim]│[/dim] "
            f"event stream active — sensing, synthesizing, evolving",
            highlight=False,
        )
        c.print()
        self._separator()
        c.print()

    async def stop(self) -> None:
        """Print the shutdown summary."""
        self._stop_status()
        self.show_streaming_end()
        elapsed = time.time() - self._started_at
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        c = self.console

        c.print()
        self._separator()
        c.print()

        # Build shutdown summary as a compact Panel
        summary_lines = [
            f"[bold]Session[/bold]   {self._session_id}",
            f"[bold]Uptime[/bold]    {mins}m {secs:02d}s",
            f"[bold]Evolved[/bold]   [green]{self._completed}[/green]  "
            f"[bold]Shed[/bold] [red]{self._failed}[/red]",
            f"[bold]Cost[/bold]      ${self._cost_total:.4f} of ${self._cost_cap:.2f}",
        ]
        panel = Panel(
            "\n".join(summary_lines),
            title="[dim]🐍 ouroboros │ dormant[/dim]",
            border_style="dim",
            width=min(c.width, 56),
            padding=(0, 2),
        )
        c.print(panel)
        c.print()

    # ══════════════════════════════════════════════════════════
    # Block infrastructure — op-scoped visual grouping
    # ══════════════════════════════════════════════════════════

    def _block_w(self) -> int:
        """Max width for block borders."""
        return min(self.console.width - 2, 70)

    def _open_op_block(self, op_id: str, sensor: str) -> None:
        """Print the top border of an op block and register it as active."""
        self._active_ops.add(op_id)
        short = _short_id(op_id)
        self._op_sensors[op_id] = sensor
        w = self._block_w()
        label = f" {short} ── {sensor} "
        pad = max(2, w - len(label) - 2)
        self.console.print(
            f"  [{_C['border']}]┌{label}{'─' * pad}[/{_C['border']}]",
            highlight=False,
        )

    def _close_op_block(self, op_id: str) -> None:
        """Print the bottom border of an op block with running stats."""
        self._active_ops.discard(op_id)
        self._op_sensors.pop(op_id, None)
        short = _short_id(op_id)
        w = self._block_w()

        stats = (
            f"🐍 [green]✅ {self._completed}[/green]  "
            f"[red]💀 {self._failed}[/red] [dim]│[/dim] "
            f"💰 ${self._cost_total:.4f}/${self._cost_cap:.2f}"
        )
        label = f" {short} ── {stats} "
        # Approximate visible length (strip markup)
        vis = _visible_len(label)
        pad = max(2, w - vis - 2)
        self.console.print(
            f"  [{_C['border']}]└{label}{'─' * pad}[/{_C['border']}]",
            highlight=False,
        )
        self.console.print()

    def _op_line(self, op_id: str, text: str) -> None:
        """Print a line within an active op block, prefixed with │.

        When multiple ops are active simultaneously, adds the short
        op_id for disambiguation.
        """
        if op_id and op_id in self._active_ops:
            if len(self._active_ops) > 1:
                short = _short_id(op_id)
                self.console.print(
                    f"  [{_C['border']}]│ {short}[/{_C['border']}] {text}",
                    highlight=False,
                )
            else:
                self.console.print(
                    f"  [{_C['border']}]│[/{_C['border']}]  {text}",
                    highlight=False,
                )
        else:
            self.console.print(f"  {text}", highlight=False)

    def _op_blank(self, op_id: str) -> None:
        """Print a blank line with the op border (visual breathing room)."""
        if op_id and op_id in self._active_ops:
            self.console.print(
                f"  [{_C['border']}]│[/{_C['border']}]",
                highlight=False,
            )
        else:
            self.console.print()

    # ── Nested blocks (tools, diffs inside ops) ──────────────

    def _open_nested(self, op_id: str, header: str) -> None:
        """Open a nested block within an op (tool call, diff, etc.)."""
        w = self._block_w() - 6  # indent for op border
        pad = max(2, w - _visible_len(header) - 4)
        border = f"[{_C['border']}]┌─ {header} {'─' * pad}[/{_C['border']}]"
        self._op_line(op_id, border)

    def _nested_line(self, op_id: str, text: str) -> None:
        """Print a line inside a nested block."""
        if op_id and op_id in self._active_ops:
            if len(self._active_ops) > 1:
                short = _short_id(op_id)
                self.console.print(
                    f"  [{_C['border']}]│ {short} │[/{_C['border']}]  {text}",
                    highlight=False,
                )
            else:
                self.console.print(
                    f"  [{_C['border']}]│  │[/{_C['border']}]  {text}",
                    highlight=False,
                )
        else:
            self.console.print(f"     {text}", highlight=False)

    def _close_nested(self, op_id: str) -> None:
        """Close a nested block."""
        w = self._block_w() - 6
        border = f"[{_C['border']}]└{'─' * w}[/{_C['border']}]"
        self._op_line(op_id, border)

    # ══════════════════════════════════════════════════════════
    # Execution masking (rich.Status spinners)
    # ══════════════════════════════════════════════════════════

    def _start_status(self, message: str, spinner: str = "dots") -> None:
        """Begin an async execution spinner.

        The spinner renders inline and vanishes when ``_stop_status`` is
        called, leaving only the final artifact printed by the caller.
        """
        self._stop_status()
        self._active_status = self.console.status(
            message, spinner=spinner, spinner_style=_C["neural"],
        )
        self._active_status.start()

    def _stop_status(self) -> None:
        """Stop the current spinner (if any) — leaves a clean terminal."""
        if self._active_status is not None:
            try:
                self._active_status.stop()
            except Exception:
                pass
            self._active_status = None

    # ══════════════════════════════════════════════════════════
    # Live Markdown streaming (rich.Live + rich.Markdown)
    # ══════════════════════════════════════════════════════════

    def show_streaming_start(self, provider: str, op_id: str = "") -> None:
        """Begin synthesis — header + open Live Markdown panel."""
        self._streaming_active = True
        self._stream_buffer = ""

        prov = _prov(provider) if provider else ""
        via_str = f" via [{_C['provider']}]{prov}[/{_C['provider']}]" if prov else ""
        self._op_line(
            op_id,
            f"[{_C['neural']}]🧬 synthesizing[/{_C['neural']}] {via_str}",
        )

        # Open Live Markdown rendering.  transient=False keeps output
        # in terminal history after the stream ends.
        self._live = Live(
            Markdown(""),
            console=self.console,
            transient=False,
            refresh_per_second=8,
        )
        self._live.start()

    def show_streaming_token(self, token: str) -> None:
        """Append a token and re-render the Markdown panel in-place."""
        if not token:
            return
        self._stream_buffer += token
        if self._live is not None:
            try:
                self._live.update(Markdown(self._stream_buffer))
            except Exception:
                pass

    def show_streaming_end(self) -> None:
        """Finalize the Live Markdown region."""
        if self._live is not None:
            try:
                if self._stream_buffer:
                    self._live.update(Markdown(self._stream_buffer))
                self._live.stop()
            except Exception:
                pass
            self._live = None
        self._stream_buffer = ""
        self._streaming_active = False

    # ══════════════════════════════════════════════════════════
    # Operation lifecycle — Zone 1 events
    # ══════════════════════════════════════════════════════════

    def op_started(
        self, op_id: str, goal: str, target_files: List[str], risk_tier: str,
        sensor: str = "",
    ) -> None:
        """A new operation was sensed — open an op block."""
        self._op_starts[op_id] = time.time()

        # Determine sensor type from goal prefix or explicit param
        sensor_label = sensor or "Operation"
        self._open_op_block(op_id, sensor_label)

        # Risk badge
        risk = risk_tier.upper() if risk_tier else ""
        if risk in ("SAFE_AUTO", "LOW"):
            risk_badge = f"[green]{risk}[/green]"
        elif risk == "MEDIUM":
            risk_badge = f"[{_C['heal']}]{risk}[/{_C['heal']}]"
        elif risk:
            risk_badge = f"[{_C['death']}]{risk}[/{_C['death']}]"
        else:
            risk_badge = "[dim]—[/dim]"

        self._op_line(
            op_id,
            f"[{_C['neural']}]🔬 sensed[/{_C['neural']}]    "
            f"{goal[:65]}",
        )
        # Risk + target files (compact)
        target_str = ""
        if target_files:
            primary = target_files[0]
            if len(primary) > 50:
                parts = primary.split("/")
                primary = "/".join(parts[-2:])
            target_str = f"  [{_C['file']}]{primary}[/{_C['file']}]"
            if len(target_files) > 1:
                target_str += f" [{_C['dim']}]+{len(target_files) - 1}[/{_C['dim']}]"

        self._op_line(
            op_id,
            f"             risk: {risk_badge}{target_str}",
        )

    def op_phase(self, op_id: str, phase: str, progress_pct: float = 0.0) -> None:
        """Phase transition — only log significant phases."""
        phase_upper = phase.upper()
        if phase_upper in ("CLASSIFY", "ROUTE", "CONTEXT_EXPANSION", "GENERATE", "VALIDATE"):
            return  # Handled by dedicated methods
        phase_map = {
            "GATE": ("🛡️", "governance gate"),
            "APPROVE": ("👤", "awaiting approval"),
            "VERIFY": ("🔍", "verifying"),
        }
        emoji, verb = phase_map.get(phase_upper, ("▸", phase.lower()))
        self._op_line(
            op_id,
            f"[{_C['neural']}]{emoji} {verb}[/{_C['neural']}]",
        )

    # ── Triage ────────────────────────────────────────────────

    def update_triage(
        self, decision: str, op_id: str = "", confidence: float = 0.0,
        reason: str = "",
    ) -> None:
        """Semantic triage decision."""
        d = decision.upper()
        color_map = {
            "PROCEED": _C["life"], "GENERATE": _C["life"],
            "NO_OP": _C["dim"], "SKIP": _C["dim"],
            "REDIRECT": _C["neural"], "ENRICH": _C["heal"],
        }
        color = color_map.get(d, "white")

        parts = f"[{color}]{d}[/{color}]"
        if confidence > 0:
            parts += f"  [{_C['dim']}]({confidence:.0%})[/{_C['dim']}]"
        if d == "NO_OP" and reason:
            parts += f"  [{_C['dim']}]{reason[:50]}[/{_C['dim']}]"

        self._op_line(
            op_id,
            f"[{_C['neural']}]🧠 triage[/{_C['neural']}]    {parts}",
        )

    # ── Provider routing ──────────────────────────────────────

    def op_provider(self, op_id: str, provider: str) -> None:
        """Provider was selected for this operation."""
        self._op_providers[op_id] = provider
        prov = _prov(provider)
        self._op_line(
            op_id,
            f"[{_C['neural']}]⚡ routing[/{_C['neural']}]    "
            f"[{_C['provider']}]{prov}[/{_C['provider']}]",
        )

    # ── Generation ────────────────────────────────────────────

    def op_generation(
        self, op_id: str, candidates: int, provider: str,
        duration_s: float = 0.0, tool_count: int = 0,
        model_id: str = "", input_tokens: int = 0, output_tokens: int = 0,
    ) -> None:
        """Generation completed — stop spinner, show summary."""
        self._stop_status()
        self.show_streaming_end()

        self._op_providers[op_id] = provider
        prov = _prov(provider)
        model_str = model_id if model_id else prov

        # Token count
        total_tokens = input_tokens + output_tokens
        token_str = f" [{_C['dim']}]│[/{_C['dim']}] {total_tokens:,} tok" if total_tokens > 0 else ""
        tools_str = f" + 🔧 {tool_count}" if tool_count > 0 else ""

        self._op_line(
            op_id,
            f"[{_C['neural']}]🧬 synthesized[/{_C['neural']}]  "
            f"{candidates} candidate{'s' if candidates != 1 else ''} via "
            f"[{_C['provider']}]{model_str}[/{_C['provider']}]"
            f"{tools_str}{token_str}"
            f"  [{_C['dim']}]({duration_s:.1f}s)[/{_C['dim']}]",
        )

    # ── Tool calls (Venom) ────────────────────────────────────

    def op_tool_start(
        self, op_id: str, tool_name: str, args_summary: str = "",
        round_index: int = 0,
    ) -> None:
        """Spin a masking spinner while a Venom tool executes."""
        tool_icons = {
            "read_file": "📄", "search_code": "🔍", "run_tests": "🧪",
            "bash": "💻", "web_search": "🌐", "web_fetch": "🌐",
            "get_callers": "🔗", "list_symbols": "📋",
        }
        icon = tool_icons.get(tool_name, "🔧")
        summary = f"  {args_summary[:40]}" if args_summary else ""

        # Include │ prefix in spinner text so it aligns with the op block
        prefix = f"  │  " if op_id in self._active_ops else "  "
        self._start_status(
            f"{prefix}{icon} T{round_index + 1} {tool_name}{summary}",
            spinner="dots",
        )

    def op_tool_call(
        self, op_id: str, tool_name: str, args_summary: str = "",
        round_index: int = 0, result_preview: str = "",
        duration_ms: float = 0.0, status: str = "success",
    ) -> None:
        """Venom tool call completed — stop spinner, print artifact."""
        self._stop_status()

        tool_icons = {
            "read_file": "📄", "search_code": "🔍", "run_tests": "🧪",
            "bash": "💻", "web_search": "🌐", "web_fetch": "🌐",
            "get_callers": "🔗", "list_symbols": "📋",
        }
        icon = tool_icons.get(tool_name, "🔧")

        dur = ""
        if duration_ms > 0:
            dur = (
                f"  [{_C['dim']}]{duration_ms:.0f}ms[/{_C['dim']}]"
                if duration_ms < 1000
                else f"  [{_C['dim']}]{duration_ms / 1000:.1f}s[/{_C['dim']}]"
            )

        status_mark = "" if status == "success" else f"  [{_C['death']}]✗[/{_C['death']}]"
        summary = f"  [{_C['dim']}]{args_summary[:40]}[/{_C['dim']}]" if args_summary else ""

        self._op_line(
            op_id,
            f"{icon} [{_C['dim']}]T{round_index + 1}[/{_C['dim']}] "
            f"{tool_name}{summary}{dur}{status_mark}",
        )

    # ── Validation ────────────────────────────────────────────

    def op_validation_start(self, op_id: str) -> None:
        """Spin a masking spinner while the immune check runs."""
        prefix = "  │  " if op_id in self._active_ops else "  "
        self._start_status(
            f"{prefix}🛡️ immune check │ running tests…",
            spinner="dots",
        )

    def op_validation(
        self, op_id: str, passed: bool, test_count: int = 0, failures: int = 0,
    ) -> None:
        """Immune check result — stop spinner, print result."""
        self._stop_status()
        if test_count == 0:
            self._op_line(
                op_id,
                f"[{_C['heal']}]🛡️ immune[/{_C['heal']}]      "
                f"[{_C['dim']}]no tests found[/{_C['dim']}]",
            )
        elif passed:
            self._op_line(
                op_id,
                f"[{_C['life']}]🛡️ immune[/{_C['life']}]      "
                f"[green]✅ {test_count}/{test_count} passing[/green]",
            )
        else:
            self._op_line(
                op_id,
                f"[{_C['death']}]🛡️ immune[/{_C['death']}]      "
                f"[red]❌ {failures}/{test_count} failing[/red]",
            )

    # ── L2 Repair ─────────────────────────────────────────────

    def op_l2_repair(
        self, op_id: str, iteration: int, max_iters: int, status: str,
    ) -> None:
        """Self-healing repair iteration."""
        color = (
            _C["life"] if status == "converged"
            else _C["heal"] if status != "failed"
            else _C["death"]
        )
        status_emoji = "✅" if status == "converged" else "🩹" if status != "failed" else "❌"

        self._op_line(
            op_id,
            f"[{_C['heal']}]🩹 repair[/{_C['heal']}]      "
            f"iter {iteration}/{max_iters}  "
            f"[{color}]{status_emoji} {status}[/{color}]",
        )

    # ── Code preview ──────────────────────────────────────────

    def show_code_preview(
        self, op_id: str, provider: str, candidate_files: List[str],
        candidate_preview: str = "", duration_s: float = 0.0,
        tool_count: int = 0, candidate_rationales: Optional[List[str]] = None,
    ) -> None:
        """Show compact candidate summary — file paths + rationale."""
        if not candidate_files and not candidate_rationales:
            return

        files = candidate_files or []
        rationales = candidate_rationales or []
        for i, fp in enumerate(files):
            if not fp:
                continue
            display_path = fp
            if len(fp) > 55:
                parts = fp.split("/")
                display_path = "/".join(parts[-2:])
            rationale = rationales[i] if i < len(rationales) else ""
            self._op_line(
                op_id,
                f"📂 [{_C['file']}]{display_path}[/{_C['file']}]",
            )
            if rationale:
                self._op_line(
                    op_id,
                    f"   [{_C['dim']}]{rationale[:70]}[/{_C['dim']}]",
                )

    # ── Diff display ──────────────────────────────────────────

    def show_diff(
        self, file_path: str, diff_text: str = "", op_id: str = "",
    ) -> None:
        """Show a colored git diff as a nested block within the op."""
        if not diff_text:
            diff_text = self._get_git_diff(file_path)

        short_path = file_path
        if len(file_path) > 45:
            parts = file_path.split("/")
            short_path = "/".join(parts[-2:])

        if not diff_text:
            self._op_line(
                op_id,
                f"[{_C['neural']}]🔗 applied[/{_C['neural']}]     "
                f"[{_C['file']}]{short_path}[/{_C['file']}]",
            )
            return

        # Open nested diff block
        self._open_nested(op_id, f"📋 {short_path}")

        # Render diff lines with color coding
        lines = diff_text.split("\n")
        if len(lines) > 60:
            lines = lines[:60]
            lines.append(f"... +{len(diff_text.split(chr(10))) - 60} lines truncated")

        for line in lines:
            if line.startswith("+++") or line.startswith("---"):
                self._nested_line(op_id, f"[{_C['dim']}]{line}[/{_C['dim']}]")
            elif line.startswith("@@"):
                self._nested_line(op_id, f"[{_C['code_hunk']}]{line}[/{_C['code_hunk']}]")
            elif line.startswith("+"):
                self._nested_line(op_id, f"[{_C['code_add']}]{line}[/{_C['code_add']}]")
            elif line.startswith("-"):
                self._nested_line(op_id, f"[{_C['code_del']}]{line}[/{_C['code_del']}]")
            else:
                self._nested_line(op_id, f"[{_C['dim']}]{line}[/{_C['dim']}]")

        self._close_nested(op_id)

    def show_diff_preview(
        self,
        diff_text: str,
        target_files: Optional[List[str]] = None,
        op_id: str = "",
    ) -> None:
        """Render a syntax-highlighted diff preview (Iron Gate / approval flow)."""
        c = self.console

        # Header
        files_str = ""
        if target_files:
            primary = target_files[0]
            if len(primary) > 50:
                parts = primary.split("/")
                primary = "/".join(parts[-2:])
            files_str = f" [{_C['file']}]{primary}[/{_C['file']}]"
            if len(target_files) > 1:
                files_str += f" [{_C['dim']}]+{len(target_files) - 1}[/{_C['dim']}]"

        c.print(
            f"  [{_C['neural']}]📋 proposed changes[/{_C['neural']}]{files_str}",
            highlight=False,
        )

        # Truncate for terminal readability
        lines = diff_text.split("\n")
        if len(lines) > 120:
            truncated = "\n".join(lines[:120])
            truncated += f"\n... +{len(lines) - 120} lines truncated"
        else:
            truncated = diff_text

        # Render with rich.syntax.Syntax — proper lexer-based highlighting
        syntax = Syntax(
            truncated,
            lexer="diff",
            theme="monokai",
            line_numbers=False,
            word_wrap=False,
            padding=(0, 1),
        )
        c.print(syntax)

    # ── Operation completion ──────────────────────────────────

    def op_completed(
        self, op_id: str, files_changed: List[str],
        provider: str = "", cost_usd: float = 0.0,
    ) -> None:
        """The organism evolved — operation succeeded."""
        self._stop_status()
        self._completed += 1
        elapsed = time.time() - self._op_starts.pop(op_id, time.time())
        prov = _prov(self._op_providers.pop(op_id, provider))
        self._cost_total += cost_usd

        # Show diffs as nested blocks
        if files_changed:
            for f in files_changed[:5]:
                self.show_diff(f, op_id=op_id)

        # Evolution line
        files_str = f"{len(files_changed)} file{'s' if len(files_changed) != 1 else ''}"
        cost_str = f" [{_C['dim']}]│[/{_C['dim']}] 💰 ${cost_usd:.4f}" if cost_usd > 0 else ""

        self._op_line(
            op_id,
            f"[{_C['life']}]✨ evolved[/{_C['life']}]     "
            f"{files_str} [{_C['dim']}]│[/{_C['dim']}] ⏱ {elapsed:.1f}s{cost_str}",
        )

        # Close the op block
        self._close_op_block(op_id)

    def op_failed(self, op_id: str, reason: str, phase: str = "") -> None:
        """The organism shed a failed change."""
        self._stop_status()
        self._failed += 1
        elapsed = time.time() - self._op_starts.pop(op_id, time.time())
        self._op_providers.pop(op_id, None)

        phase_str = f" at [{_C['neural']}]{phase}[/{_C['neural']}]" if phase else ""

        self._op_line(
            op_id,
            f"[{_C['death']}]💀 shed[/{_C['death']}]        "
            f"[{_C['death']}]{reason[:70]}[/{_C['death']}]{phase_str}"
            f"  [{_C['dim']}]⏱ {elapsed:.1f}s[/{_C['dim']}]",
        )
        self._op_line(
            op_id,
            f"[{_C['dim']}]             the organism will learn from this failure[/{_C['dim']}]",
        )

        # Close the op block
        self._close_op_block(op_id)

    def op_noop(self, op_id: str, reason: str = "") -> None:
        """Triage NO_OP — operation was unnecessary."""
        self._op_starts.pop(op_id, None)
        self._op_providers.pop(op_id, None)
        reason_str = f"  [{_C['dim']}]{reason[:50]}[/{_C['dim']}]" if reason else ""

        self._op_line(
            op_id,
            f"[{_C['dim']}]⏭️  no-op{reason_str}[/{_C['dim']}]",
        )

        # Close the op block (it's done)
        self._close_op_block(op_id)

    # ══════════════════════════════════════════════════════════
    # Organism intelligence updates
    # ══════════════════════════════════════════════════════════

    def update_intent_discovery(self, cycle: int, submitted: int) -> None:
        """IntentDiscoverySensor found something."""
        self.console.print(
            f"  [{_C['neural']}]🧬 discovery[/{_C['neural']}]  "
            f"cycle {cycle} — {submitted} intent{'s' if submitted != 1 else ''} submitted",
            highlight=False,
        )

    def update_dream_engine(self, blueprints: int, title: str = "") -> None:
        """DreamEngine produced a blueprint."""
        title_str = f'  "{title[:40]}"' if title else ""
        self.console.print(
            f"  [{_C['neural']}]💭 dreaming[/{_C['neural']}]   "
            f"{blueprints} blueprint{'s' if blueprints != 1 else ''}{title_str}",
            highlight=False,
        )

    def update_learning(self, rules: int, trend: str = "→") -> None:
        """Learning consolidation update."""
        self.console.print(
            f"  [{_C['neural']}]📖 learning[/{_C['neural']}]   "
            f"{rules} rules consolidated  trend: {trend}",
            highlight=False,
        )

    def update_cost(
        self, total: float, remaining: float, breakdown: Dict[str, float],
    ) -> None:
        """Cost tick — shown periodically between operations."""
        self._cost_total = total

    def update_sensors(self, count: int) -> None:
        """Update active sensor count (tracked for status bar)."""
        self._sensors_active = count

    def update_provider_chain(self, chain: str) -> None:
        """Show the provider chain (displayed in boot banner, not inline)."""
        pass  # Handled by boot_banner now

    # ══════════════════════════════════════════════════════════
    # Proactive event interruptions
    # ══════════════════════════════════════════════════════════

    def emit_proactive_alert(
        self,
        title: str,
        body: str,
        severity: str = "warning",
        source: str = "",
        op_id: str = "",
    ) -> None:
        """Inject a prominent alert Panel into the terminal stream.

        Because the REPL runs under ``prompt_toolkit.patch_stdout``, all
        writes through Rich's Console are automatically rendered *above*
        the active input line.
        """
        color_map = {
            "critical": _C["death"],
            "warning": _C["heal"],
            "info": _C["neural"],
        }
        border = color_map.get(severity, _C["neural"])
        icon_map = {"critical": "🚨", "warning": "⚠️", "info": "🔔"}
        icon = icon_map.get(severity, "🔔")

        subtitle_parts: List[str] = []
        if source:
            subtitle_parts.append(source)
        if op_id:
            subtitle_parts.append(f"op:{_short_id(op_id)}")
        subtitle = (
            f"[{_C['dim']}]{' │ '.join(subtitle_parts)}[/{_C['dim']}]"
            if subtitle_parts else ""
        )

        panel = Panel(
            body,
            title=f"{icon} {title}",
            subtitle=subtitle,
            border_style=border,
            expand=False,
            width=min(self.console.width, 68),
            padding=(0, 1),
        )
        self.console.print()
        self.console.print(panel)
        self.console.print()

    # ══════════════════════════════════════════════════════════
    # Iron Gate permission prompt
    # ══════════════════════════════════════════════════════════

    async def request_execution_permission(
        self,
        op_id: str,
        description: str,
        target_files: List[str],
        risk_reason: str = "",
        diff_text: str = "",
        candidate_rationale: str = "",
    ) -> bool:
        """Interactive [Y/n] permission gate — Manifesto §6 Iron Gate.

        Pauses the calling agentic coroutine and renders:
          1. A color-coded diff preview
          2. An alert Panel summarizing the proposed change
          3. A ``prompt_toolkit`` async prompt awaiting ``[Y/n]``

        Returns ``True`` for approval, ``False`` for rejection.
        """
        short = _short_id(op_id) if op_id else ""
        c = self.console

        # Step 1: Diff preview
        if diff_text:
            self.show_diff_preview(
                diff_text=diff_text,
                target_files=target_files,
                op_id=op_id,
            )

        # Step 2: Iron Gate panel
        body_lines = [f"[bold]{description}[/bold]"]
        if target_files:
            files_display = ", ".join(
                f.split("/")[-1] if "/" in f else f for f in target_files[:5]
            )
            body_lines.append(f"📂 {files_display}")
        if candidate_rationale:
            body_lines.append(f"[{_C['dim']}]{candidate_rationale[:120]}[/{_C['dim']}]")
        if risk_reason:
            body_lines.append(f"[{_C['heal']}]⚡ {risk_reason}[/{_C['heal']}]")

        panel = Panel(
            "\n".join(body_lines),
            title=f"🔒 Iron Gate │ op:{short}",
            border_style=_C["heal"],
            expand=False,
            width=min(c.width, 68),
            padding=(0, 1),
        )
        c.print()
        c.print(panel)

        # Step 3: Async [Y/n] prompt
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.formatted_text import HTML
            from prompt_toolkit.patch_stdout import patch_stdout

            session = PromptSession()
            with patch_stdout():
                answer = await session.prompt_async(
                    HTML("<b>  Apply this change? [Y/n] </b>"),
                )
            answer = answer.strip().lower()
            approved = answer in ("", "y", "yes")
        except ImportError:
            c.print(
                f"  [{_C['heal']}](prompt_toolkit unavailable — auto-approving)[/{_C['heal']}]",
                highlight=False,
            )
            approved = True
        except (EOFError, KeyboardInterrupt):
            approved = False

        # Step 4: Decision artifact
        if approved:
            c.print(
                f"  [{_C['life']}]✅ approved[/{_C['life']}]  [{_C['dim']}]op:{short}[/{_C['dim']}]",
                highlight=False,
            )
        else:
            c.print(
                f"  [{_C['death']}]❌ rejected[/{_C['death']}]  [{_C['dim']}]op:{short}[/{_C['dim']}]",
                highlight=False,
            )
        c.print()
        return approved

    # ══════════════════════════════════════════════════════════
    # Helpers
    # ══════════════════════════════════════════════════════════

    def _separator(self) -> None:
        """Full-width separator between major sections."""
        width = min(self.console.width, 70)
        self.console.print(
            f"  [{_C['dim']}]{'━' * width}[/{_C['dim']}]",
            highlight=False,
        )

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


# ══════════════════════════════════════════════════════════════
# SerpentTransport — CommProtocol adapter
# ══════════════════════════════════════════════════════════════


class SerpentTransport:
    """CommProtocol transport that routes messages to SerpentFlow.

    Drop-in replacement for DashboardTransport. Wired into
    CommProtocol._transports by the battle test harness.
    """

    def __init__(self, flow: SerpentFlow) -> None:
        self._flow = flow
        self._op_providers: Dict[str, str] = {}
        self._boot_recovery_count: int = 0
        self._boot_recovery_flushed: bool = False
        # Dedup: track which ops already displayed validation/synthesizing
        self._validation_shown: set = set()
        self._synthesizing_shown: set = set()

    async def send(self, msg: Any) -> None:
        """Handle a CommMessage and render via SerpentFlow."""
        try:
            payload = msg.payload if hasattr(msg, "payload") else {}
            op_id = msg.op_id if hasattr(msg, "op_id") else ""
            msg_type = msg.msg_type.value if hasattr(msg, "msg_type") else ""

            if msg_type == "INTENT":
                # Flush boot recovery summary before first real operation
                if self._boot_recovery_count > 0 and not self._boot_recovery_flushed:
                    self._boot_recovery_flushed = True
                    self._flow.console.print(
                        f"  [{_C['dim']}]⏭️  boot recovery │ "
                        f"{self._boot_recovery_count} stale entries reconciled[/{_C['dim']}]",
                        highlight=False,
                    )
                    self._flow.console.print()

                if payload.get("risk_tier") not in ("routing",):
                    self._validation_shown.discard(op_id)
                    self._synthesizing_shown.discard(op_id)
                    # Detect sensor type from payload
                    sensor = payload.get("outcome_source", "") or payload.get("sensor", "")
                    if not sensor:
                        goal = payload.get("goal", "")
                        if "test" in goal.lower():
                            sensor = "TestFailure"
                        elif "gap" in goal.lower():
                            sensor = "CapabilityGap"
                        else:
                            sensor = "Operation"
                    self._flow.op_started(
                        op_id=op_id,
                        goal=payload.get("goal", ""),
                        target_files=payload.get("target_files", []),
                        risk_tier=payload.get("risk_tier", ""),
                        sensor=sensor,
                    )

            elif msg_type == "HEARTBEAT":
                phase = payload.get("phase", "")

                # Triage decision
                if phase == "semantic_triage" and payload.get("triage_decision"):
                    self._flow.update_triage(
                        decision=payload["triage_decision"],
                        op_id=op_id,
                        confidence=payload.get("triage_confidence", 0.0),
                        reason=payload.get("triage_reason", ""),
                    )
                    if payload["triage_decision"].upper() == "NO_OP":
                        self._flow.op_noop(op_id, payload.get("triage_reason", ""))

                # Tool call — two-phase: start (spin) then complete (artifact)
                elif payload.get("tool_name"):
                    if payload.get("tool_starting"):
                        self._flow.op_tool_start(
                            op_id=op_id,
                            tool_name=payload["tool_name"],
                            args_summary=payload.get("tool_args_summary", ""),
                            round_index=payload.get("round_index", 0),
                        )
                    else:
                        self._flow.op_tool_call(
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
                    self._flow.op_generation(
                        op_id=op_id,
                        candidates=payload["candidates_count"],
                        provider=provider,
                        duration_s=payload.get("generation_duration_s", 0.0),
                        tool_count=payload.get("tool_records", 0),
                        model_id=payload.get("model_id", ""),
                        input_tokens=payload.get("total_input_tokens", 0),
                        output_tokens=payload.get("total_output_tokens", 0),
                    )
                    candidate_files = payload.get("candidate_files", [])
                    candidate_rationales = payload.get("candidate_rationales", [])
                    if candidate_files or candidate_rationales:
                        self._flow.show_code_preview(
                            op_id=op_id,
                            provider=provider,
                            candidate_files=candidate_files,
                            candidate_rationales=candidate_rationales,
                        )

                # Validation — dedup: show once per op
                elif phase.upper() in ("VALIDATE", "VALIDATE_RETRY") and "test_passed" in payload:
                    if op_id not in self._validation_shown:
                        self._validation_shown.add(op_id)
                        self._flow.op_validation(
                            op_id=op_id,
                            passed=payload.get("test_passed", False),
                            test_count=payload.get("test_count", 0),
                            failures=payload.get("test_failures", 0),
                        )

                # Validation phase starting — spin masking spinner
                elif phase.upper() == "VALIDATE" and "test_passed" not in payload:
                    if op_id not in self._validation_shown:
                        self._flow.op_validation_start(op_id=op_id)

                # L2 repair
                elif payload.get("l2_iteration") is not None:
                    self._flow.op_l2_repair(
                        op_id=op_id,
                        iteration=payload["l2_iteration"],
                        max_iters=payload.get("l2_max_iters", 5),
                        status=payload.get("l2_status", ""),
                    )

                # APPLY phase — show real-time diffs
                elif phase.upper() == "APPLY" and payload.get("target_file"):
                    self._flow.show_diff(
                        file_path=payload["target_file"],
                        diff_text=payload.get("diff_text", ""),
                        op_id=op_id,
                    )

                # Streaming — dedup: show synthesizing once per op
                elif payload.get("streaming") == "start":
                    if op_id not in self._synthesizing_shown:
                        self._synthesizing_shown.add(op_id)
                        provider = payload.get("provider", "unknown")
                        self._op_providers[op_id] = provider
                        self._flow.show_streaming_start(provider=provider, op_id=op_id)
                elif payload.get("streaming") == "token":
                    self._flow.show_streaming_token(payload.get("token", ""))
                elif payload.get("streaming") == "end":
                    self._flow.show_streaming_end()

                # IntentDiscovery sensor
                elif payload.get("intent_discovery_cycle") is not None:
                    self._flow.update_intent_discovery(
                        cycle=payload["intent_discovery_cycle"],
                        submitted=payload.get("intent_discovery_submitted", 0),
                    )

                # DreamEngine
                elif payload.get("dream_blueprints") is not None:
                    self._flow.update_dream_engine(
                        blueprints=payload["dream_blueprints"],
                        title=payload.get("dream_title", ""),
                    )

                # Proactive alert
                elif payload.get("proactive_alert"):
                    self._flow.emit_proactive_alert(
                        title=payload.get("alert_title", "Alert"),
                        body=payload.get("alert_body", ""),
                        severity=payload.get("alert_severity", "warning"),
                        source=payload.get("alert_source", ""),
                        op_id=op_id,
                    )

                # Standard phase transition
                elif phase and ":" not in phase:
                    self._flow.op_phase(
                        op_id=op_id,
                        phase=phase,
                        progress_pct=payload.get("progress_pct", 0.0),
                    )

            elif msg_type == "DECISION":
                outcome = payload.get("outcome", "")
                reason_code = payload.get("reason_code", "")

                # Suppress boot_recovery spam
                if reason_code.startswith("boot_recovery_"):
                    self._boot_recovery_count += 1
                    if self._boot_recovery_count == 1:
                        self._flow.console.print(
                            f"  [{_C['dim']}]⏭️  boot recovery │ "
                            f"reconciling stale ledger entries...[/{_C['dim']}]",
                            highlight=False,
                        )
                    return

                # Escalation — emit proactive alert
                if outcome == "escalated":
                    self._flow.emit_proactive_alert(
                        title="Iron Gate Escalation",
                        body=f"Operation escalated to APPROVAL_REQUIRED.\n"
                             f"Reason: {reason_code}\n"
                             f"Files: {', '.join(payload.get('target_files', [])[:3])}",
                        severity="warning",
                        source="GovernanceGate",
                        op_id=op_id,
                    )
                    return

                files = payload.get("files_changed", payload.get("affected_files", []))
                provider = self._op_providers.pop(op_id, "unknown")

                if outcome in ("completed", "applied", "auto_approved"):
                    self._flow.op_completed(
                        op_id=op_id,
                        files_changed=files,
                        provider=provider,
                        cost_usd=payload.get("cost_usd", 0.0),
                    )
                elif outcome in ("failed", "postmortem"):
                    self._flow.op_failed(
                        op_id=op_id,
                        reason=reason_code or outcome,
                        phase=payload.get("failed_phase", ""),
                    )

            elif msg_type == "POSTMORTEM":
                self._flow.op_failed(
                    op_id=op_id,
                    reason=payload.get("root_cause", "unknown"),
                    phase=payload.get("failed_phase", ""),
                )

        except Exception:
            pass  # The serpent never crashes the pipeline


# ══════════════════════════════════════════════════════════════
# SerpentApprovalProvider — Iron Gate wired to prompt_toolkit
# ══════════════════════════════════════════════════════════════


class SerpentApprovalProvider:
    """Approval provider that renders diff + Iron Gate prompt via SerpentFlow.

    Wraps the standard ``CLIApprovalProvider`` and overrides the
    approval flow to:

    1. Generate a unified diff of the proposed change
    2. Render it with ``rich.syntax.Syntax(lexer="diff")``
    3. Present an interactive ``[Y/n]`` prompt via ``prompt_toolkit``
    4. Route the decision back through the standard provider
    """

    def __init__(self, flow: SerpentFlow, inner: Any) -> None:
        self._flow = flow
        self._inner = inner  # CLIApprovalProvider

    async def request(self, context: Any) -> str:
        """Delegate request registration to the inner provider."""
        return await self._inner.request(context)

    async def await_decision(
        self, request_id: str, timeout_s: float,
    ) -> Any:
        """Show diff + Iron Gate prompt, then route decision to inner provider."""
        pending = self._inner._requests.get(request_id)
        if pending is None or pending.result is not None:
            return await self._inner.await_decision(request_id, timeout_s)

        ctx = pending.context
        op_id = ctx.op_id
        description = ctx.description or ""
        target_files = list(ctx.target_files) if ctx.target_files else []

        # Generate proposed diff from candidate
        diff_text = ""
        candidate_rationale = ""
        try:
            candidate: Dict[str, Any] = {}
            _val = getattr(ctx, "validation", None)
            if _val is not None:
                candidate = getattr(_val, "best_candidate", None) or {}
            if not candidate:
                _gen = getattr(ctx, "generation", None)
                if _gen is not None and getattr(_gen, "candidates", None):
                    candidate = _gen.candidates[0] if _gen.candidates else {}

            proposed = candidate.get("full_content", "")
            candidate_rationale = (candidate.get("rationale", "") or "")[:120]
            if proposed and target_files:
                import difflib
                _repo = self._flow._repo_path
                _target = _repo / target_files[0]
                if _target.exists():
                    _original = _target.read_text(errors="replace")
                    if _original != proposed:
                        diff_lines = difflib.unified_diff(
                            _original.splitlines(keepends=True),
                            proposed.splitlines(keepends=True),
                            fromfile=f"a/{target_files[0]}",
                            tofile=f"b/{target_files[0]}",
                            lineterm="",
                        )
                        diff_text = "\n".join(diff_lines)
        except Exception:
            pass

        risk_reason = getattr(ctx, "terminal_reason_code", "") or ""
        approved = await self._flow.request_execution_permission(
            op_id=op_id,
            description=description,
            target_files=target_files,
            risk_reason=risk_reason,
            diff_text=diff_text,
            candidate_rationale=candidate_rationale,
        )

        if approved:
            return await self._inner.approve(request_id, "operator")
        else:
            return await self._inner.reject(request_id, "operator", "rejected via Iron Gate")

    async def list_pending(self) -> List[Dict[str, Any]]:
        """Delegate to inner provider."""
        return await self._inner.list_pending()


# ══════════════════════════════════════════════════════════════
# SerpentREPL — Non-blocking async REPL with status bar
# ══════════════════════════════════════════════════════════════


class SerpentREPL:
    """Non-blocking REPL with persistent status bar (Zone 2 + Zone 3).

    Uses ``prompt_toolkit.PromptSession.prompt_async()`` with a
    ``bottom_toolbar`` that displays live organism metrics:
    active ops, cost, evolved/shed counts, uptime.

    Parameters
    ----------
    flow:
        SerpentFlow instance — used for styled output and status data.
    on_command:
        Async callback invoked with each line of user input.
    prompt_str:
        The prompt string shown to the user.
    """

    def __init__(
        self,
        flow: SerpentFlow,
        on_command: Optional[Callable[[str], Any]] = None,
        prompt_str: str = "🐍 ouroboros > ",
    ) -> None:
        self._flow = flow
        self._on_command = on_command
        self._prompt_str = prompt_str
        self._session: Any = None
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        """Start the REPL loop as a background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        """Gracefully shut down the REPL."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        """Async REPL loop with persistent bottom toolbar."""
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.formatted_text import HTML
            from prompt_toolkit.patch_stdout import patch_stdout
        except ImportError:
            self._flow.console.print(
                f"  [{_C['dim']}]REPL disabled: prompt_toolkit not installed[/{_C['dim']}]",
                highlight=False,
            )
            return

        # ── Zone 3: Status bar via bottom_toolbar ──
        # The toolbar callable is invoked on every prompt redraw,
        # giving us a live-updating status line at the terminal bottom.
        def _toolbar():
            f = self._flow
            elapsed = time.time() - f._started_at
            m, s = int(elapsed // 60), int(elapsed % 60)
            active = len(f._active_ops)

            active_str = f"{active} active" if active else "idle"
            return HTML(
                f" <b>🐍</b> │ "
                f"<style fg='ansigreen'>✅ {f._completed}</style>  "
                f"<style fg='ansired'>💀 {f._failed}</style> │ "
                f"💰 ${f._cost_total:.3f}/${f._cost_cap:.2f} │ "
                f"📡 {f._sensors_active} sensors │ "
                f"{active_str} │ "
                f"⏱ {m}m {s:02d}s"
            )

        self._session = PromptSession(bottom_toolbar=_toolbar)

        with patch_stdout():
            while self._running:
                try:
                    line = await self._session.prompt_async(
                        HTML(f"<b>{self._prompt_str}</b>"),
                    )
                    line = line.strip()
                    if not line:
                        continue

                    # Built-in commands
                    if line in ("quit", "exit", "q"):
                        self._flow.console.print(
                            f"  [{_C['dim']}]Shutting down…[/{_C['dim']}]",
                            highlight=False,
                        )
                        self._running = False
                        break
                    if line == "status":
                        self._print_status()
                        continue
                    if line == "help":
                        self._print_help()
                        continue

                    # Delegate to external handler
                    if self._on_command is not None:
                        try:
                            result = self._on_command(line)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as exc:
                            self._flow.console.print(
                                f"  [{_C['death']}]Error: {exc}[/{_C['death']}]",
                                highlight=False,
                            )
                except EOFError:
                    break
                except KeyboardInterrupt:
                    continue
                except asyncio.CancelledError:
                    break

    def _print_status(self) -> None:
        """Print detailed organism status."""
        f = self._flow
        elapsed = time.time() - f._started_at
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)

        lines = [
            f"[bold]Session[/bold]     {f._session_id}",
            f"[bold]Uptime[/bold]      {mins}m {secs:02d}s",
            f"[bold]Evolved[/bold]     [green]{f._completed}[/green]",
            f"[bold]Shed[/bold]        [red]{f._failed}[/red]",
            f"[bold]Cost[/bold]        ${f._cost_total:.4f} / ${f._cost_cap:.2f}",
            f"[bold]Active Ops[/bold]  {len(f._active_ops)}",
            f"[bold]Sensors[/bold]     {f._sensors_active}",
        ]
        panel = Panel(
            "\n".join(lines),
            title="[cyan]🐍 Organism Status[/cyan]",
            border_style="cyan",
            width=min(f.console.width, 50),
            padding=(0, 2),
        )
        f.console.print()
        f.console.print(panel)
        f.console.print()

    def _print_help(self) -> None:
        """Print available REPL commands."""
        lines = [
            f"  [{_C['dim']}]status[/{_C['dim']}]    organism status panel",
            f"  [{_C['dim']}]cost[/{_C['dim']}]      cost breakdown",
            f"  [{_C['dim']}]help[/{_C['dim']}]      this message",
            f"  [{_C['dim']}]quit[/{_C['dim']}]      graceful shutdown",
        ]
        panel = Panel(
            "\n".join(lines),
            title="[cyan]🐍 Commands[/cyan]",
            border_style="dim",
            width=min(self._flow.console.width, 44),
            padding=(0, 1),
        )
        self._flow.console.print()
        self._flow.console.print(panel)
        self._flow.console.print()
