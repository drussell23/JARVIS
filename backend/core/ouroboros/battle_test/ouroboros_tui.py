"""OuroborosTUI — Claude Code-level interactive terminal for the battle test.

Professional Rich-powered TUI showing real-time:
- Provider routing (🔵 DW / 🟡 Claude / 🟠 GCP)
- Venom tool calls with syntax-highlighted results
- Red/green colored diffs during APPLY
- L2 repair iterations with error context
- Cost tracking per provider
- Ouroboros + Venom + Consciousness commit signature

Manifesto §7: Absolute Observability — the inner workings of the symbiote
must be entirely visible.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

# ══════════════════════════════════════════════════════════════════
# Provider Badges
# ══════════════════════════════════════════════════════════════════

_PROVIDER_BADGES: Dict[str, str] = {
    "doubleword-397b": "\U0001f535 Tier 0: DoubleWord 397B ($0.10/M)",
    "doubleword": "\U0001f535 Tier 0: DoubleWord 397B ($0.10/M)",
    "claude-api": "\U0001f7e1 Tier 1: Claude Sonnet ($3/M)",
    "claude": "\U0001f7e1 Tier 1: Claude Sonnet ($3/M)",
    "gcp-jprime": "\U0001f7e0 Tier 2: GCP J-Prime",
}

_TOOL_ICONS: Dict[str, str] = {
    "read_file": "\U0001f4c4",
    "search_code": "\U0001f50d",
    "run_tests": "\U0001f9ea",
    "bash": "\U0001f4bb",
    "web_search": "\U0001f310",
    "web_fetch": "\U0001f310",
    "list_symbols": "\U0001f3f7\ufe0f",
    "get_callers": "\U0001f517",
    "code_explore": "\U0001f9e0",
    "lsp_check": "\U0001f4d0",
}

_PHASE_ICONS: Dict[str, str] = {
    "classify": "\U0001f50d",
    "route": "\U0001f9ed",
    "context_expansion": "\U0001f4da",
    "generate": "\u2728",
    "validate": "\U0001f9ea",
    "gate": "\U0001f6e1\ufe0f",
    "approve": "\U0001f464",
    "apply": "\U0001f4be",
    "verify": "\u2705",
    "complete": "\U0001f389",
}


# ══════════════════════════════════════════════════════════════════
# OuroborosConsole — the main TUI renderer
# ══════════════════════════════════════════════════════════════════


class OuroborosConsole:
    """Rich-powered console for the Ouroboros battle test.

    Renders professional, emoji-rich output showing exactly what the
    organism is doing at every step — which provider, which tools,
    what code, what diffs. Like Claude Code but for an autonomous
    self-developing organism.
    """

    def __init__(self, repo_path: Path) -> None:
        self._console = Console(emoji=True, highlight=False)
        self._repo_path = repo_path
        self._expand_mode = False  # Ctrl+O toggles
        self._show_diffs = True
        self._show_explore = True
        self._op_start_times: Dict[str, float] = {}
        self._op_costs: Dict[str, float] = {}

    @property
    def console(self) -> Console:
        return self._console

    def toggle_expand(self) -> None:
        self._expand_mode = not self._expand_mode
        mode = "EXPANDED" if self._expand_mode else "COMPACT"
        self._console.print(
            f"  [dim]\u2699\ufe0f  Display mode: {mode}[/dim]",
        )

    def toggle_diffs(self) -> None:
        self._show_diffs = not self._show_diffs
        self._console.print(
            f"  [dim]\U0001f4dd Diffs: {'ON' if self._show_diffs else 'OFF'}[/dim]",
        )

    def toggle_explore(self) -> None:
        self._show_explore = not self._show_explore
        self._console.print(
            f"  [dim]\U0001f50d Explore: {'ON' if self._show_explore else 'OFF'}[/dim]",
        )

    # ── Operation lifecycle ────────────────────────────────────

    def show_operation_start(
        self,
        op_id: str,
        goal: str,
        target_files: List[str],
        risk_tier: str,
    ) -> None:
        """Show operation header panel."""
        self._op_start_times[op_id] = time.time()
        short_id = op_id.split("-")[1][:8] if "-" in op_id else op_id[:8]

        risk_color = {
            "SAFE_AUTO": "green",
            "LOW": "green",
            "MEDIUM": "yellow",
            "HIGH": "red",
            "CRITICAL": "bold red",
        }.get(risk_tier, "white")

        files_text = "\n".join(f"     \u2192 {f}" for f in target_files[:5])
        if len(target_files) > 5:
            files_text += f"\n     [dim]... +{len(target_files) - 5} more[/dim]"

        content = (
            f"\U0001f4cb {goal}\n"
            f"\U0001f4c2 Target files:\n{files_text}"
        )

        self._console.print()
        self._console.print(Panel(
            content,
            title=f"\U0001f40d OUROBOROS  op:{short_id}",
            subtitle=f"[{risk_color}]{risk_tier}[/{risk_color}]",
            border_style="cyan",
            padding=(0, 2),
        ))

    def show_provider(self, provider_name: str, has_venom: bool = False) -> None:
        """Show which API provider is being used."""
        badge = _PROVIDER_BADGES.get(provider_name, f"\u26aa {provider_name}")
        venom = " \u2014 [bold]Venom tool loop active[/bold]" if has_venom else ""
        self._console.print(f"\n  {badge}{venom}\n")

    # ── Tool calls ─────────────────────────────────────────────

    def show_tool_call(
        self,
        op_id: str,
        tool_name: str,
        args_summary: str = "",
        round_index: int = 0,
        result_preview: str = "",
        duration_ms: float = 0.0,
        status: str = "success",
    ) -> None:
        """Show a Venom tool call with optional result preview."""
        icon = _TOOL_ICONS.get(tool_name, "\U0001f527")
        short_id = op_id.split("-")[1][:8] if "-" in op_id else op_id[:8]
        args_display = f"  [dim]{args_summary[:80]}[/dim]" if args_summary else ""

        self._console.print(
            f"  {icon} [cyan]Turn {round_index + 1}:[/cyan] "
            f"[bold]{tool_name}[/bold]{args_display}"
            f"  [dim]op:{short_id}[/dim]"
        )

        # Show result preview if in expand mode or explore mode
        if result_preview and (self._expand_mode or self._show_explore):
            lines = result_preview.strip().split("\n")
            if len(lines) > 8 and not self._expand_mode:
                preview = "\n".join(lines[:5])
                preview += f"\n     [dim]... +{len(lines) - 5} more lines[/dim]"
            else:
                preview = "\n".join(lines[:20])
                if len(lines) > 20:
                    preview += f"\n     [dim]... +{len(lines) - 20} more lines[/dim]"

            # Syntax highlight based on tool type
            if tool_name in ("read_file", "list_symbols") and len(preview) > 10:
                try:
                    syntax = Syntax(
                        preview, "python", theme="monokai",
                        line_numbers=False, word_wrap=True,
                    )
                    self._console.print(Panel(
                        syntax,
                        border_style="dim",
                        padding=(0, 3),
                    ))
                except Exception:
                    self._console.print(f"     [dim]{preview}[/dim]")
            elif tool_name == "bash":
                self._console.print(f"     [dim]\u2514\u2500 $ {args_summary}[/dim]")
                if preview:
                    self._console.print(f"     [dim]{preview[:200]}[/dim]")
            elif tool_name == "search_code":
                for line in lines[:5]:
                    self._console.print(f"     [dim]\u251c\u2500 {line.strip()}[/dim]")
            elif tool_name == "run_tests":
                self._console.print(f"     [dim]\u2514\u2500 {preview[:200]}[/dim]")
            else:
                self._console.print(f"     [dim]\u2514\u2500 {preview[:200]}[/dim]")

        # Duration/status footer
        if duration_ms > 0:
            dur_str = f"{duration_ms:.0f}ms" if duration_ms < 1000 else f"{duration_ms / 1000:.1f}s"
            status_icon = "\u2713" if status == "success" else "\u2717"
            self._console.print(f"     [dim]{status_icon} ({dur_str})[/dim]")

    # ── Generation ─────────────────────────────────────────────

    def show_thinking(self, text: str) -> None:
        """Show provider reasoning/thinking."""
        self._console.print(f"  [dim italic]\U0001f4ad {text}[/dim italic]")

    # ── Streaming output ────────────────────────────────────

    def show_streaming_token(self, token: str) -> None:
        """Show a streaming token chunk (character-by-character generation).

        Called by the provider's streaming callback for each token.
        Prints without newline to create the character-by-character
        effect — like Claude Code showing code as it's being written.
        """
        # Print token without newline, flush immediately
        sys.stdout.write(token)
        sys.stdout.flush()

    def show_streaming_start(self, provider: str) -> None:
        """Show that streaming generation is starting."""
        badge = _PROVIDER_BADGES.get(provider, provider)
        self._console.print(f"\n  \u2728 [dim]Generating via {badge}...[/dim]")
        # Print indented prefix for the streaming content
        sys.stdout.write("  \033[2m")
        sys.stdout.flush()

    def show_streaming_end(self) -> None:
        """End the streaming output block."""
        sys.stdout.write("\033[0m\n")
        sys.stdout.flush()

    def show_generation_result(
        self,
        op_id: str,
        candidates: int,
        provider: str,
        duration_s: float = 0.0,
        tool_count: int = 0,
        candidate_files: Optional[List[str]] = None,
        candidate_preview: str = "",
    ) -> None:
        """Show generation completion with code preview."""
        short_id = op_id.split("-")[1][:8] if "-" in op_id else op_id[:8]
        badge = _PROVIDER_BADGES.get(provider, provider)
        tool_str = f", {tool_count} tool calls" if tool_count > 0 else ""

        self._console.print(
            f"\n  \U0001f9ec [bold green]{candidates} candidate(s)[/bold green] "
            f"via {badge}"
            f"  [dim]({duration_s:.1f}s{tool_str})[/dim]"
            f"  [dim]op:{short_id}[/dim]"
        )

        # Show candidate file paths
        if candidate_files:
            for f in candidate_files[:5]:
                if f:
                    self._console.print(f"     \U0001f4c4 [cyan]{f}[/cyan]")

        # Show code preview if expanded or always show first 200 chars
        if candidate_preview:
            preview = candidate_preview[:300] if not self._expand_mode else candidate_preview[:1000]
            if preview:
                try:
                    syntax = Syntax(
                        preview, "json", theme="monokai",
                        line_numbers=False, word_wrap=True,
                    )
                    self._console.print(Panel(
                        syntax,
                        title="\u2728 Generated Code Preview",
                        border_style="green",
                        padding=(0, 1),
                    ))
                except Exception:
                    self._console.print(f"  [dim]{preview}[/dim]")

        self._console.print()

    # ── Validation ─────────────────────────────────────────────

    def show_validation(
        self,
        op_id: str,
        passed: bool,
        test_count: int = 0,
        failures: int = 0,
        output_preview: str = "",
    ) -> None:
        """Show test validation result."""
        short_id = op_id.split("-")[1][:8] if "-" in op_id else op_id[:8]
        if passed:
            self._console.print(
                f"  \U0001f9ea [bold green]VALIDATE: {test_count} tests passed \u2705[/bold green]"
                f"  [dim]op:{short_id}[/dim]"
            )
        else:
            self._console.print(
                f"  \U0001f9ea [bold red]VALIDATE: {failures}/{test_count} "
                f"failed \u274c[/bold red]  [dim]op:{short_id}[/dim]"
            )
            if output_preview and self._expand_mode:
                self._console.print(f"     [dim]{output_preview[:300]}[/dim]")

    # ── L2 Repair ──────────────────────────────────────────────

    def show_l2_repair(
        self,
        op_id: str,
        iteration: int,
        max_iters: int,
        status: str,
        error_text: str = "",
        fix_preview: str = "",
    ) -> None:
        """Show L2 repair engine progress."""
        short_id = op_id.split("-")[1][:8] if "-" in op_id else op_id[:8]
        progress = f"[{iteration}/{max_iters}]"

        if status == "converged":
            self._console.print(
                f"  \U0001f527 [bold green]L2 Repair {progress}: CONVERGED \u2705[/bold green]"
                f"  [dim]op:{short_id}[/dim]"
            )
        elif status == "failed":
            self._console.print(
                f"  \U0001f527 [bold red]L2 Repair {progress}: {status}[/bold red]"
                f"  [dim]op:{short_id}[/dim]"
            )
        else:
            self._console.print(
                f"  \U0001f527 [yellow]L2 Repair {progress}:[/yellow] {status}"
                f"  [dim]op:{short_id}[/dim]"
            )

        if error_text and self._expand_mode:
            self._console.print(f"     [dim red]\u2514\u2500 Error: {error_text[:200]}[/dim red]")
        if fix_preview and self._expand_mode:
            self._console.print(f"     [dim green]\u2514\u2500 Fix: {fix_preview[:200]}[/dim green]")

    # ── Diff display ───────────────────────────────────────────

    def show_diff(self, file_path: str, diff_text: str = "") -> None:
        """Show colored diff for a file."""
        if not self._show_diffs:
            self._console.print(f"  \U0001f4be [green]APPLY:[/green] {file_path}")
            return

        if not diff_text:
            # Try to get diff from git
            diff_text = self._get_git_diff(file_path)

        if diff_text:
            try:
                syntax = Syntax(
                    diff_text, "diff", theme="monokai",
                    line_numbers=True, word_wrap=True,
                )
                self._console.print(Panel(
                    syntax,
                    title=f"\U0001f4dd {file_path}",
                    border_style="green",
                    padding=(0, 1),
                ))
            except Exception:
                # Fallback to manual coloring
                self._console.print(f"  \U0001f4be [green]APPLY:[/green] {file_path}")
        else:
            self._console.print(f"  \U0001f4be [green]APPLY:[/green] {file_path}")

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

    # ── Phase transitions ──────────────────────────────────────

    def show_phase(self, op_id: str, phase: str, progress_pct: float = 0.0) -> None:
        """Show a phase transition."""
        icon = _PHASE_ICONS.get(phase.lower(), "\u25b6\ufe0f")
        short_id = op_id.split("-")[1][:8] if "-" in op_id else op_id[:8]
        elapsed = time.time() - self._op_start_times.get(op_id, time.time())

        self._console.print(
            f"  {icon} [bold]{phase.upper()}[/bold]"
            f"  [dim]({elapsed:.0f}s, {progress_pct:.0f}%)[/dim]"
            f"  [dim]op:{short_id}[/dim]"
        )

    # ── Results ────────────────────────────────────────────────

    def show_complete(
        self,
        op_id: str,
        duration_s: float,
        files_changed: List[str],
        provider: str,
        cost_usd: float = 0.0,
    ) -> None:
        """Show operation success panel."""
        short_id = op_id.split("-")[1][:8] if "-" in op_id else op_id[:8]
        badge = _PROVIDER_BADGES.get(provider, provider)

        # Show diffs for changed files
        if files_changed and self._show_diffs:
            for f in files_changed[:5]:
                self.show_diff(f)

        cost_str = f"\U0001f4b0 ${cost_usd:.4f}" if cost_usd > 0 else ""

        self._console.print(Panel(
            Text.from_markup(
                f"Generated-By: [bold]Ouroboros + Venom + Consciousness[/bold]\n"
                f"Signed-off-by: [dim]JARVIS Ouroboros <ouroboros@jarvis.local>[/dim]\n"
                f"Provider: {badge}"
            ),
            title=f"\u2705 [bold green]SUCCESS[/bold green]  \u23f1 {duration_s:.1f}s  {cost_str}",
            subtitle=f"[dim]op:{short_id}[/dim]",
            border_style="green",
            padding=(0, 2),
        ))

    def show_failed(
        self,
        op_id: str,
        reason: str,
        phase: str = "",
        duration_s: float = 0.0,
    ) -> None:
        """Show operation failure panel."""
        short_id = op_id.split("-")[1][:8] if "-" in op_id else op_id[:8]
        phase_str = f" at {phase}" if phase else ""

        self._console.print(Panel(
            Text.from_markup(
                f"[red]Reason: {reason}[/red]{phase_str}\n"
                f"[dim]The organism will learn from this failure.[/dim]"
            ),
            title=f"\u274c [bold red]FAILED[/bold red]  \u23f1 {duration_s:.1f}s",
            subtitle=f"[dim]op:{short_id}[/dim]",
            border_style="red",
            padding=(0, 2),
        ))

    # ── Cost ───────────────────────────────────────────────────

    def show_cost_update(
        self,
        total: float,
        remaining: float,
        breakdown: Dict[str, float],
    ) -> None:
        """Show cost tracker update."""
        parts = [f"{k}: ${v:.4f}" for k, v in breakdown.items()]
        self._console.print(
            f"  \U0001f4b0 [dim]Cost: ${total:.4f} "
            f"({', '.join(parts)}) "
            f"\u2014 ${remaining:.2f} remaining[/dim]"
        )

    # ── Controls bar ───────────────────────────────────────────

    def show_controls_bar(self) -> None:
        """Show the keyboard controls bar."""
        self._console.print(
            f"\n  [dim]"
            f"[Ctrl+O: {'collapse' if self._expand_mode else 'expand'}] "
            f"[Ctrl+B: background] "
            f"[e: explore {'OFF' if not self._show_explore else 'ON'}] "
            f"[d: diffs {'OFF' if not self._show_diffs else 'ON'}]"
            f"[/dim]\n"
        )


# ══════════════════════════════════════════════════════════════════
# OuroborosTUITransport — CommProtocol transport adapter
# ══════════════════════════════════════════════════════════════════


class OuroborosTUITransport:
    """CommProtocol transport that routes messages to OuroborosConsole.

    Replaces BattleDiffTransport with Rich-powered rendering.
    """

    def __init__(self, tui: OuroborosConsole) -> None:
        self._tui = tui
        self._current_provider: Dict[str, str] = {}  # op_id → provider name

    async def send(self, msg: Any) -> None:
        """Handle a CommMessage with Rich TUI output."""
        try:
            payload = msg.payload if hasattr(msg, "payload") else {}
            op_id = msg.op_id if hasattr(msg, "op_id") else ""
            msg_type = msg.msg_type.value if hasattr(msg, "msg_type") else ""

            if msg_type == "INTENT":
                if payload.get("risk_tier") not in ("routing",):
                    self._tui.show_operation_start(
                        op_id=op_id,
                        goal=payload.get("goal", ""),
                        target_files=payload.get("target_files", []),
                        risk_tier=payload.get("risk_tier", ""),
                    )

            elif msg_type == "HEARTBEAT":
                phase = payload.get("phase", "")

                # Tool call display
                if payload.get("tool_name"):
                    self._tui.show_tool_call(
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
                    self._current_provider[op_id] = provider
                    self._tui.show_provider(
                        provider,
                        has_venom=payload.get("tool_records", 0) > 0,
                    )
                    self._tui.show_generation_result(
                        op_id=op_id,
                        candidates=payload["candidates_count"],
                        provider=provider,
                        duration_s=payload.get("generation_duration_s", 0.0),
                        tool_count=payload.get("tool_records", 0),
                        candidate_files=payload.get("candidate_files", []),
                        candidate_preview=payload.get("candidate_preview", ""),
                    )

                # Validation result
                elif phase.upper() in ("VALIDATE", "VALIDATE_RETRY") and "test_passed" in payload:
                    self._tui.show_validation(
                        op_id=op_id,
                        passed=payload.get("test_passed", False),
                        test_count=payload.get("test_count", 0),
                        failures=payload.get("test_failures", 0),
                        output_preview=payload.get("validation_output", ""),
                    )

                # L2 repair
                elif payload.get("l2_iteration") is not None:
                    self._tui.show_l2_repair(
                        op_id=op_id,
                        iteration=payload["l2_iteration"],
                        max_iters=payload.get("l2_max_iters", 5),
                        status=payload.get("l2_status", ""),
                        error_text=payload.get("l2_error", ""),
                        fix_preview=payload.get("l2_fix_preview", ""),
                    )

                # File apply with diff
                elif phase.upper() == "APPLY" and payload.get("target_file"):
                    self._tui.show_diff(
                        payload["target_file"],
                        diff_text=payload.get("diff_text", ""),
                    )

                # Standard phase transition
                elif phase and ":" not in phase:
                    self._tui.show_phase(
                        op_id=op_id,
                        phase=phase,
                        progress_pct=payload.get("progress_pct", 0.0),
                    )

            elif msg_type == "DECISION":
                outcome = payload.get("outcome", "")
                elapsed = time.time() - self._tui._op_start_times.pop(op_id, time.time())
                files = payload.get("files_changed", payload.get("affected_files", []))
                provider = self._current_provider.pop(op_id, "unknown")

                if outcome in ("completed", "applied", "auto_approved"):
                    self._tui.show_complete(
                        op_id=op_id,
                        duration_s=elapsed,
                        files_changed=files,
                        provider=provider,
                    )
                elif outcome in ("failed", "postmortem"):
                    self._tui.show_failed(
                        op_id=op_id,
                        reason=payload.get("reason_code", outcome),
                        phase=payload.get("failed_phase", ""),
                        duration_s=elapsed,
                    )

            elif msg_type == "POSTMORTEM":
                self._tui.show_failed(
                    op_id=op_id,
                    reason=payload.get("root_cause", "unknown"),
                    phase=payload.get("failed_phase", ""),
                )

        except Exception:
            pass  # TUI should never crash the pipeline


# ══════════════════════════════════════════════════════════════════
# KeyboardHandler — async non-blocking input
# ══════════════════════════════════════════════════════════════════


class KeyboardHandler:
    """Non-blocking keyboard input handler for interactive controls.

    Ctrl+O: Toggle expand/collapse detail level
    Ctrl+B: Send session to background
    e: Toggle explore mode (show tool result details)
    d: Toggle diff display
    """

    def __init__(
        self,
        tui: OuroborosConsole,
        shutdown_event: Optional[asyncio.Event] = None,
    ) -> None:
        self._tui = tui
        self._shutdown_event = shutdown_event
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._old_settings = None

    async def start(self) -> None:
        """Start listening for keyboard input."""
        if sys.stdin.isatty():
            self._running = True
            self._task = asyncio.create_task(
                self._input_loop(), name="keyboard_handler",
            )

    async def stop(self) -> None:
        """Stop the keyboard handler and restore terminal."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _input_loop(self) -> None:
        """Read keyboard input in a non-blocking loop."""
        loop = asyncio.get_running_loop()
        try:
            # Save terminal settings
            self._old_settings = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())

            while self._running:
                # Non-blocking read via executor
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

                # Ctrl+O (ASCII 15) — toggle expand
                if char == "\x0f":
                    self._tui.toggle_expand()
                # Ctrl+B (ASCII 2) — background
                elif char == "\x02":
                    self._tui.console.print(
                        "\n  [bold yellow]\U0001f504 Session sent to background. "
                        "Use `fg` to resume.[/bold yellow]\n"
                    )
                    import os, signal
                    os.kill(os.getpid(), signal.SIGTSTP)
                # 'e' — toggle explore
                elif char == "e":
                    self._tui.toggle_explore()
                # 'd' — toggle diffs
                elif char == "d":
                    self._tui.toggle_diffs()

        except Exception:
            pass
        finally:
            # Restore terminal settings
            if self._old_settings is not None:
                try:
                    termios.tcsetattr(
                        sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings,
                    )
                except Exception:
                    pass
