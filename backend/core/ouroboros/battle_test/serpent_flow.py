"""Serpent Flow — Ouroboros Flowing CLI with Organism Personality.

The serpent doesn't pin a dashboard to your terminal. It flows.
Output streams naturally downward — sensing, synthesizing, evolving —
like watching a living organism think in real time.

Manifesto §7: Absolute Observability — the inner workings of the
symbiote must be entirely visible.

Design:
  - Console.print() flows downward — scannable, grep-friendly
  - Async execution masking via rich.Status (spinners vanish on completion)
  - Live Markdown streaming via rich.Live + rich.Markdown during synthesis
  - Non-blocking REPL via prompt_toolkit.PromptSession.prompt_async()
  - Organism vocabulary: sensed, synthesizing, immune check, evolved, shed
  - Inline syntax-highlighted diffs (Claude Code style + serpent personality)
  - Emoji + color coding for scannable readability
"""
from __future__ import annotations

import asyncio
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
    # Strip the "op-" prefix and repo suffix, flatten hyphens
    raw = op_id
    if raw.startswith("op-"):
        raw = raw[3:]
    # Remove trailing repo name (e.g. "-jarvis")
    # UUIDv7 is 32 hex + 4 hyphens = 36 chars
    hex_only = raw.replace("-", "")
    # Skip first 12 hex chars (timestamp), take 6 from the random portion
    if len(hex_only) > 18:
        return hex_only[12:18]
    # Fallback: last 6 chars
    return hex_only[-6:] if len(hex_only) >= 6 else hex_only


def _prov(provider: str) -> str:
    """Normalize provider name for display."""
    return _PROV.get(provider, provider[:12])


# ══════════════════════════════════════════════════════════════
# SerpentFlow — the flowing organism CLI
# ══════════════════════════════════════════════════════════════


class SerpentFlow:
    """Ouroboros flowing CLI with organism personality.

    No pinned dashboard. No terminal muting. Just a living organism
    streaming its thoughts down your terminal.

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

        # Rich console
        self.console = Console(emoji=True, highlight=False)

        # ── Execution masking (rich.Status) ──────────────────
        # Active spinner — only one at a time.  `_stop_status()` clears it
        # before printing the completion artifact so the spinner vanishes.
        self._active_status: Optional[Status] = None

        # ── Live Markdown streaming (rich.Live + rich.Markdown) ──
        # Accumulates tokens during synthesis; rendered in real-time.
        self._live: Optional[Live] = None
        self._stream_buffer: str = ""

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        """Print the awakening banner."""
        c = self.console
        c.print()
        c.print(f"[{_C['life']}]🐍 ouroboros[/{_C['life']}] │ awakening", highlight=False)
        c.print(f"             │ 💰 budget: ${self._cost_cap:.2f} │ idle timeout: {self._idle_timeout_s:.0f}s", highlight=False)
        if self._branch_name:
            c.print(f"             │ 🌿 branch: [{_C['dim']}]{self._branch_name}[/{_C['dim']}]", highlight=False)
        if self._session_id:
            c.print(f"             │ [{_C['dim']}]session: {self._session_id}[/{_C['dim']}]", highlight=False)
        c.print()
        self._separator()
        c.print()

    async def stop(self) -> None:
        """Print the shutdown summary."""
        # Clean up any active spinner or live stream before final output
        self._stop_status()
        self.show_streaming_end()
        elapsed = time.time() - self._started_at
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        c = self.console
        c.print()
        self._separator()
        c.print()
        c.print(
            f"[{_C['life']}]🐍 ouroboros[/{_C['life']}] │ dormant",
            highlight=False,
        )
        c.print(
            f"             │ ⏱ {mins}m {secs:02d}s │ "
            f"[green]✅ {self._completed} evolved[/green]  "
            f"[red]💀 {self._failed} shed[/red]  "
            f"💰 ${self._cost_total:.4f} of ${self._cost_cap:.2f}",
            highlight=False,
        )
        c.print()

    def update_sensors(self, count: int) -> None:
        """Update active sensor count (shown in awakening)."""
        self._sensors_active = count
        self.console.print(
            f"             │ 📡 {count} sensors active",
            highlight=False,
        )

    def update_provider_chain(self, chain: str) -> None:
        """Show the provider chain at boot."""
        self.console.print(
            f"             │ ⚡ chain: [{_C['provider']}]{chain}[/{_C['provider']}]",
            highlight=False,
        )

    # ── Operation lifecycle ───────────────────────────────────

    def op_started(
        self, op_id: str, goal: str, target_files: List[str], risk_tier: str,
    ) -> None:
        """A new operation was sensed."""
        short = _short_id(op_id)
        self._op_starts[op_id] = time.time()

        # Risk badge
        risk = risk_tier.upper() if risk_tier else ""
        if risk in ("SAFE_AUTO", "LOW"):
            risk_badge = f"[green]{risk}[/green]"
        elif risk == "MEDIUM":
            risk_badge = f"[{_C['heal']}]{risk}[/{_C['heal']}]"
        elif risk:
            risk_badge = f"[{_C['death']}]{risk}[/{_C['death']}]"
        else:
            risk_badge = ""

        target_str = ""
        if target_files:
            primary = target_files[0]
            if len(primary) > 50:
                parts = primary.split("/")
                primary = "/".join(parts[-2:])
            target_str = f"\n          │ 📂 [{_C['file']}]{primary}[/{_C['file']}]"
            if len(target_files) > 1:
                target_str += f" [{_C['dim']}]+{len(target_files)-1} more[/{_C['dim']}]"

        self.console.print(
            f"[{_C['neural']}]🔬 sensed[/{_C['neural']}] │ "
            f"{goal[:70]}  [{_C['dim']}]op:{short}[/{_C['dim']}]"
            f"\n          │ risk: {risk_badge}"
            f"{target_str}",
            highlight=False,
        )
        self.console.print()

    def op_phase(self, op_id: str, phase: str, progress_pct: float = 0.0) -> None:
        """Phase transition — only log significant phases."""
        # Skip noisy transitions; the interesting ones have dedicated methods
        phase_upper = phase.upper()
        if phase_upper in ("CLASSIFY", "ROUTE", "CONTEXT_EXPANSION", "GENERATE", "VALIDATE"):
            return  # Handled by dedicated methods (synthesizing/synthesized, immune check)
        short = _short_id(op_id)

        phase_map = {
            "GATE": ("🛡️", "governance gate"),
            "APPROVE": ("👤", "awaiting approval"),
            "VERIFY": ("🔍", "verifying integration"),
        }
        emoji, verb = phase_map.get(phase_upper, ("▸", phase.lower()))
        self.console.print(
            f"[{_C['neural']}]{emoji} {verb}[/{_C['neural']}] │ "
            f"[{_C['dim']}]op:{short}[/{_C['dim']}]",
            highlight=False,
        )

    # ── Triage ────────────────────────────────────────────────

    def update_triage(
        self, decision: str, op_id: str = "", confidence: float = 0.0,
        reason: str = "",
    ) -> None:
        """Semantic triage decision."""
        short = _short_id(op_id) if op_id else ""
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

        id_str = f"  [{_C['dim']}]op:{short}[/{_C['dim']}]" if short else ""
        self.console.print(
            f"[{_C['neural']}]🧠 triage[/{_C['neural']}] │ {parts}{id_str}",
            highlight=False,
        )

    # ── Provider routing ──────────────────────────────────────

    def op_provider(self, op_id: str, provider: str) -> None:
        """Provider was selected for this operation."""
        self._op_providers[op_id] = provider
        short = _short_id(op_id)
        prov = _prov(provider)
        self.console.print(
            f"[{_C['neural']}]⚡ routing[/{_C['neural']}] │ "
            f"[{_C['provider']}]{prov}[/{_C['provider']}]"
            f"  [{_C['dim']}]op:{short}[/{_C['dim']}]",
            highlight=False,
        )

    # ── Generation ────────────────────────────────────────────

    def op_generation(
        self, op_id: str, candidates: int, provider: str,
        duration_s: float = 0.0, tool_count: int = 0,
        model_id: str = "", input_tokens: int = 0, output_tokens: int = 0,
    ) -> None:
        """Generation completed — stop spinner, show summary with model + token count."""
        # Stop any active execution-masking spinner (synthesis or tool)
        self._stop_status()
        # Stop live Markdown stream if still running
        self.show_streaming_end()

        short = _short_id(op_id)
        prov = _prov(provider)
        self._op_providers[op_id] = provider

        # Model display: show model_id if available, otherwise provider name
        model_str = model_id if model_id else prov

        # Token count
        total_tokens = input_tokens + output_tokens
        if total_tokens > 0:
            token_str = f" │ {total_tokens:,} tokens"
        else:
            token_str = ""

        tools_str = f" + 🔧 {tool_count} tools" if tool_count > 0 else ""

        self.console.print(
            f"[{_C['neural']}]🧬 synthesized[/{_C['neural']}] │ "
            f"{candidates} candidate{'s' if candidates != 1 else ''} via "
            f"[{_C['provider']}]{model_str}[/{_C['provider']}]"
            f"{tools_str}{token_str}"
            f"  [{_C['dim']}]({duration_s:.1f}s)  op:{short}[/{_C['dim']}]",
            highlight=False,
        )

    # ── Tool calls (Venom) ────────────────────────────────────

    def op_tool_start(
        self, op_id: str, tool_name: str, args_summary: str = "",
        round_index: int = 0,
    ) -> None:
        """Spin a masking spinner while a Venom tool executes.

        Called *before* the tool runs.  The spinner is replaced by the
        final artifact line when ``op_tool_call`` fires on completion.
        """
        tool_icons = {
            "read_file": "📄", "search_code": "🔍", "run_tests": "🧪",
            "bash": "💻", "web_search": "🌐", "web_fetch": "🌐",
            "get_callers": "🔗", "list_symbols": "📋",
        }
        icon = tool_icons.get(tool_name, "🔧")
        summary = f"  {args_summary[:45]}" if args_summary else ""
        self._start_status(
            f"          │ {icon} T{round_index+1} {tool_name}{summary}",
            spinner="dots",
        )

    def op_tool_call(
        self, op_id: str, tool_name: str, args_summary: str = "",
        round_index: int = 0, result_preview: str = "",
        duration_ms: float = 0.0, status: str = "success",
    ) -> None:
        """Venom tool call completed — stop spinner, print clean artifact."""
        # Stop the execution masking spinner before printing the artifact
        self._stop_status()

        tool_icons = {
            "read_file": "📄", "search_code": "🔍", "run_tests": "🧪",
            "bash": "💻", "web_search": "🌐", "web_fetch": "🌐",
            "get_callers": "🔗", "list_symbols": "📋",
        }
        icon = tool_icons.get(tool_name, "🔧")

        dur = ""
        if duration_ms > 0:
            dur = f"  [{_C['dim']}]{duration_ms:.0f}ms[/{_C['dim']}]" if duration_ms < 1000 else f"  [{_C['dim']}]{duration_ms/1000:.1f}s[/{_C['dim']}]"

        status_mark = "" if status == "success" else f"  [{_C['death']}]✗[/{_C['death']}]"
        summary = f"  [{_C['dim']}]{args_summary[:45]}[/{_C['dim']}]" if args_summary else ""

        self.console.print(
            f"          │ {icon} [{_C['dim']}]T{round_index+1}[/{_C['dim']}] "
            f"{tool_name}{summary}{dur}{status_mark}",
            highlight=False,
        )

    # ── Validation ────────────────────────────────────────────

    def op_validation_start(self, op_id: str) -> None:
        """Spin a masking spinner while the immune check runs."""
        short = _short_id(op_id)
        self._start_status(
            f"🛡️ immune check │ running tests…  [dim]op:{short}[/dim]",
            spinner="dots",
        )

    def op_validation(
        self, op_id: str, passed: bool, test_count: int = 0, failures: int = 0,
    ) -> None:
        """Immune check result — stop spinner, print clean artifact."""
        self._stop_status()
        short = _short_id(op_id)
        if test_count == 0:
            # No tests discovered — show neutral status
            self.console.print(
                f"[{_C['heal']}]🛡️ immune check[/{_C['heal']}] │ "
                f"[{_C['dim']}]no tests found[/{_C['dim']}]"
                f"  [{_C['dim']}]op:{short}[/{_C['dim']}]",
                highlight=False,
            )
        elif passed:
            self.console.print(
                f"[{_C['life']}]🛡️ immune check[/{_C['life']}] │ "
                f"[green]✅ {test_count}/{test_count} tests passing[/green]"
                f"  [{_C['dim']}]op:{short}[/{_C['dim']}]",
                highlight=False,
            )
        else:
            self.console.print(
                f"[{_C['death']}]🛡️ immune check[/{_C['death']}] │ "
                f"[red]❌ {failures}/{test_count} tests failing[/red]"
                f"  [{_C['dim']}]op:{short}[/{_C['dim']}]",
                highlight=False,
            )

    # ── L2 Repair ─────────────────────────────────────────────

    def op_l2_repair(
        self, op_id: str, iteration: int, max_iters: int, status: str,
    ) -> None:
        """Self-healing repair iteration."""
        short = _short_id(op_id)
        color = _C["life"] if status == "converged" else _C["heal"] if status != "failed" else _C["death"]
        status_emoji = "✅" if status == "converged" else "🩹" if status != "failed" else "❌"

        self.console.print(
            f"[{_C['heal']}]🩹 repairing[/{_C['heal']}] │ "
            f"iteration {iteration}/{max_iters}  "
            f"[{color}]{status_emoji} {status}[/{color}]"
            f"  [{_C['dim']}]op:{short}[/{_C['dim']}]",
            highlight=False,
        )

    # ── Code preview (syntax highlighted) ─────────────────────

    def show_code_preview(
        self, op_id: str, provider: str, candidate_files: List[str],
        candidate_preview: str = "", duration_s: float = 0.0,
        tool_count: int = 0, candidate_rationales: Optional[List[str]] = None,
    ) -> None:
        """Show compact candidate summary — file paths + rationale, not raw content."""
        c = self.console
        if not candidate_files and not candidate_rationales:
            return

        # Show each candidate as a compact card: file + rationale
        files = candidate_files or []
        rationales = candidate_rationales or []
        for i, fp in enumerate(files):
            if not fp:
                continue
            # Shorten long paths
            display_path = fp
            if len(fp) > 55:
                parts = fp.split("/")
                display_path = "/".join(parts[-2:])
            rationale = rationales[i] if i < len(rationales) else ""
            c.print(
                f"          │ 📂 [{_C['file']}]{display_path}[/{_C['file']}]",
                highlight=False,
            )
            if rationale:
                c.print(
                    f"          │ [{_C['dim']}]{rationale[:75]}[/{_C['dim']}]",
                    highlight=False,
                )

    # ── Diff display (CC-style + organism personality) ─────────

    def show_diff(
        self, file_path: str, diff_text: str = "", op_id: str = "",
    ) -> None:
        """Show a colored git diff inline — Claude Code style with serpent personality."""
        short = _short_id(op_id) if op_id else ""
        c = self.console

        # Get diff from git if not provided
        if not diff_text:
            diff_text = self._get_git_diff(file_path)

        if not diff_text:
            c.print(
                f"[{_C['neural']}]🔗 assimilating[/{_C['neural']}] │ "
                f"[{_C['file']}]{file_path}[/{_C['file']}]"
                + (f"  [{_C['dim']}]op:{short}[/{_C['dim']}]" if short else ""),
                highlight=False,
            )
            return

        # Header
        c.print(
            f"[{_C['neural']}]🔗 assimilating[/{_C['neural']}] │ "
            f"[{_C['file']}]{file_path}[/{_C['file']}]"
            + (f"  [{_C['dim']}]op:{short}[/{_C['dim']}]" if short else ""),
            highlight=False,
        )

        # Render diff lines with color coding (CC style)
        lines = diff_text.split("\n")
        if len(lines) > 80:
            lines = lines[:80]
            lines.append(f"... +{len(diff_text.split(chr(10))) - 80} lines truncated")

        for line in lines:
            if line.startswith("+++") or line.startswith("---"):
                c.print(f"          [{_C['dim']}]{line}[/{_C['dim']}]", highlight=False)
            elif line.startswith("@@"):
                c.print(f"          [{_C['code_hunk']}]{line}[/{_C['code_hunk']}]", highlight=False)
            elif line.startswith("+"):
                c.print(f"          [{_C['code_add']}]{line}[/{_C['code_add']}]", highlight=False)
            elif line.startswith("-"):
                c.print(f"          [{_C['code_del']}]{line}[/{_C['code_del']}]", highlight=False)
            else:
                c.print(f"          [{_C['dim']}]{line}[/{_C['dim']}]", highlight=False)

        c.print()

    # ── Execution masking (rich.Status spinners) ────────────────

    def _start_status(self, message: str, spinner: str = "dots") -> None:
        """Begin an async execution spinner.

        The spinner renders inline and vanishes when ``_stop_status`` is
        called, leaving only the final artifact printed by the caller.
        Only one spinner is active at a time; starting a new one stops
        the previous.
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

    # ── Live Markdown streaming (rich.Live + rich.Markdown) ───

    def show_streaming_start(self, provider: str, op_id: str = "") -> None:
        """Begin synthesis — spin a masking spinner, then open a Live Markdown panel."""
        self._streaming_active = True
        self._stream_buffer = ""

        short = _short_id(op_id) if op_id else ""
        id_str = f"  [dim]op:{short}[/dim]" if short else ""
        prov = _prov(provider) if provider else ""
        via_str = f" via [{_C['provider']}]{prov}[/{_C['provider']}]" if prov else ""

        # Print the header (permanent artifact)
        self.console.print(
            f"[{_C['neural']}]🧬 synthesizing[/{_C['neural']}] │{via_str}{id_str}",
            highlight=False,
        )

        # Open a rich.Live context for real-time Markdown rendering.
        # transient=True makes the live region vanish when stopped,
        # but we refresh with a final snapshot first so the output persists.
        self._live = Live(
            Markdown(""),
            console=self.console,
            transient=False,
            refresh_per_second=8,
        )
        self._live.start()

    def show_streaming_token(self, token: str) -> None:
        """Append a token and re-render the Markdown panel in-place.

        Called from the provider's SSE/streaming callback.  Each call
        updates the ``rich.Live`` region so the terminal shows a
        progressively-rendered Markdown document.
        """
        if not token:
            return
        self._stream_buffer += token
        if self._live is not None:
            try:
                self._live.update(Markdown(self._stream_buffer))
            except Exception:
                pass

    def show_streaming_end(self) -> None:
        """Finalize the Live Markdown region.

        Performs one final render so the accumulated text remains visible,
        then stops the Live widget cleanly.
        """
        if self._live is not None:
            try:
                # Final render — keeps the content in terminal history
                if self._stream_buffer:
                    self._live.update(Markdown(self._stream_buffer))
                self._live.stop()
            except Exception:
                pass
            self._live = None
        self._stream_buffer = ""
        self._streaming_active = False

    # ── Operation completion ──────────────────────────────────

    def op_completed(
        self, op_id: str, files_changed: List[str],
        provider: str = "", cost_usd: float = 0.0,
    ) -> None:
        """The organism evolved — operation succeeded."""
        self._stop_status()  # Clean up any lingering spinner
        self._completed += 1
        short = _short_id(op_id)
        elapsed = time.time() - self._op_starts.pop(op_id, time.time())
        prov = _prov(self._op_providers.pop(op_id, provider))
        self._cost_total += cost_usd
        c = self.console

        # Show diffs for changed files
        if files_changed:
            for f in files_changed[:5]:
                self.show_diff(f, op_id=op_id)

        # Evolution announcement
        files_str = f"{len(files_changed)} file{'s' if len(files_changed) != 1 else ''}"
        cost_str = f" │ 💰 ${cost_usd:.4f}" if cost_usd > 0 else ""

        c.print(
            f"[{_C['life']}]✨ evolved[/{_C['life']}] │ "
            f"[{_C['dim']}]op:{short}[/{_C['dim']}] │ "
            f"[{_C['life']}]{files_str} changed[/{_C['life']}] │ "
            f"⏱ {elapsed:.1f}s{cost_str}",
            highlight=False,
        )
        c.print(
            f"          │ [{_C['dim']}]Generated-By: Ouroboros + Venom + Consciousness[/{_C['dim']}]",
            highlight=False,
        )
        c.print()
        self._cycle_separator()

    def op_failed(self, op_id: str, reason: str, phase: str = "") -> None:
        """The organism shed a failed change."""
        self._stop_status()  # Clean up any lingering spinner
        self._failed += 1
        short = _short_id(op_id)
        elapsed = time.time() - self._op_starts.pop(op_id, time.time())
        self._op_providers.pop(op_id, None)
        c = self.console

        phase_str = f" at [{_C['neural']}]{phase}[/{_C['neural']}]" if phase else ""

        c.print(
            f"[{_C['death']}]💀 shed[/{_C['death']}] │ "
            f"[{_C['dim']}]op:{short}[/{_C['dim']}]{phase_str} │ "
            f"⏱ {elapsed:.1f}s",
            highlight=False,
        )
        c.print(
            f"        │ [{_C['death']}]{reason[:80]}[/{_C['death']}]",
            highlight=False,
        )
        c.print(
            f"        │ [{_C['dim']}]the organism will learn from this failure[/{_C['dim']}]",
            highlight=False,
        )
        c.print()
        self._cycle_separator()

    def op_noop(self, op_id: str, reason: str = "") -> None:
        """Triage NO_OP — operation was unnecessary."""
        short = _short_id(op_id)
        self._op_starts.pop(op_id, None)
        self._op_providers.pop(op_id, None)
        reason_str = f"  [{_C['dim']}]{reason[:50]}[/{_C['dim']}]" if reason else ""
        self.console.print(
            f"[{_C['dim']}]⏭️  no-op[/{_C['dim']}] │ "
            f"[{_C['dim']}]op:{short}{reason_str}[/{_C['dim']}]",
            highlight=False,
        )

    # ── Organism intelligence updates ─────────────────────────

    def update_intent_discovery(self, cycle: int, submitted: int) -> None:
        """IntentDiscoverySensor found something."""
        self.console.print(
            f"[{_C['neural']}]🧬 discovery[/{_C['neural']}] │ "
            f"cycle {cycle} — {submitted} intent{'s' if submitted != 1 else ''} submitted",
            highlight=False,
        )

    def update_dream_engine(self, blueprints: int, title: str = "") -> None:
        """DreamEngine produced a blueprint."""
        title_str = f'  "{title[:40]}"' if title else ""
        self.console.print(
            f"[{_C['neural']}]💭 dreaming[/{_C['neural']}] │ "
            f"{blueprints} blueprint{'s' if blueprints != 1 else ''}{title_str}",
            highlight=False,
        )

    def update_learning(self, rules: int, trend: str = "→") -> None:
        """Learning consolidation update."""
        self.console.print(
            f"[{_C['neural']}]📖 learning[/{_C['neural']}] │ "
            f"{rules} rules consolidated  trend: {trend}",
            highlight=False,
        )

    def update_cost(
        self, total: float, remaining: float, breakdown: Dict[str, float],
    ) -> None:
        """Cost tick — shown periodically between operations."""
        self._cost_total = total

    # ── Proactive event interruptions ────────────────────────

    def emit_proactive_alert(
        self,
        title: str,
        body: str,
        severity: str = "warning",
        source: str = "",
        op_id: str = "",
    ) -> None:
        """Inject a prominent alert Panel into the terminal stream.

        Called by background tasks (sensors, consciousness, health cortex)
        when they detect an event that demands the operator's attention.

        Because the REPL runs under ``prompt_toolkit.patch_stdout``, all
        writes through Rich's Console are automatically buffered and
        rendered *above* the active input line — the operator's typing
        is never interrupted.

        Parameters
        ----------
        title:
            Short headline (e.g. ``"Capability Gap Detected"``).
        body:
            Multi-line detail (Markdown-safe).
        severity:
            ``"critical"`` (red), ``"warning"`` (yellow), or ``"info"`` (cyan).
        source:
            Originating subsystem (e.g. ``"CapabilityGapSensor"``).
        op_id:
            Related operation ID, if any.
        """
        color_map = {
            "critical": _C["death"],
            "warning": _C["heal"],
            "info": _C["neural"],
        }
        border = color_map.get(severity, _C["neural"])
        icon_map = {
            "critical": "🚨",
            "warning": "⚠️",
            "info": "🔔",
        }
        icon = icon_map.get(severity, "🔔")

        subtitle_parts: List[str] = []
        if source:
            subtitle_parts.append(source)
        if op_id:
            subtitle_parts.append(f"op:{_short_id(op_id)}")
        subtitle = f"[{_C['dim']}]{' │ '.join(subtitle_parts)}[/{_C['dim']}]" if subtitle_parts else ""

        panel = Panel(
            body,
            title=f"{icon} {title}",
            subtitle=subtitle,
            border_style=border,
            expand=False,
            width=min(self.console.width, 72),
            padding=(0, 1),
        )
        self.console.print()
        self.console.print(panel)
        self.console.print()

    # ── Iron Gate permission prompt ──────────────────────────

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
          1. A color-coded diff preview (``rich.syntax.Syntax``)
          2. An alert Panel summarizing the proposed change
          3. A ``prompt_toolkit`` async prompt awaiting ``[Y/n]``

        Returns ``True`` for approval, ``False`` for rejection.
        The caller's ``asyncio.Task`` is suspended (not the event loop)
        so background telemetry, sensors, and streaming continue.

        Parameters
        ----------
        op_id:
            Operation requesting permission.
        description:
            Human-readable goal of the operation.
        target_files:
            Files to be modified.
        risk_reason:
            Why the Iron Gate was triggered (e.g. "similarity_escalation").
        diff_text:
            Unified diff of proposed changes.  If non-empty, rendered as a
            syntax-highlighted preview before the prompt.
        candidate_rationale:
            LLM rationale for the change.
        """
        short = _short_id(op_id) if op_id else ""
        c = self.console

        # ── Step 1: Live diff preview (rich.syntax.Syntax, lexer="diff") ──
        if diff_text:
            self.show_diff_preview(
                diff_text=diff_text,
                target_files=target_files,
                op_id=op_id,
            )

        # ── Step 2: Iron Gate alert panel ──
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
            width=min(c.width, 72),
            padding=(0, 1),
        )
        c.print()
        c.print(panel)

        # ── Step 3: Async [Y/n] prompt ──
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
            # No prompt_toolkit — auto-approve with warning
            c.print(
                f"[{_C['heal']}]  (prompt_toolkit unavailable — auto-approving)[/{_C['heal']}]",
                highlight=False,
            )
            approved = True
        except (EOFError, KeyboardInterrupt):
            approved = False

        # ── Step 4: Print decision artifact ──
        if approved:
            c.print(
                f"[{_C['life']}]  ✅ approved[/{_C['life']}] │ "
                f"[{_C['dim']}]op:{short}[/{_C['dim']}]",
                highlight=False,
            )
        else:
            c.print(
                f"[{_C['death']}]  ❌ rejected[/{_C['death']}] │ "
                f"[{_C['dim']}]op:{short}[/{_C['dim']}]",
                highlight=False,
            )
        c.print()
        return approved

    # ── Live diff preview (rich.syntax.Syntax, lexer="diff") ─

    def show_diff_preview(
        self,
        diff_text: str,
        target_files: Optional[List[str]] = None,
        op_id: str = "",
    ) -> None:
        """Render a syntax-highlighted diff preview.

        Uses ``rich.syntax.Syntax`` with ``lexer="diff"`` so additions
        are green, deletions are red, and hunk headers are styled —
        proper terminal typography per Manifesto §7.

        Called by ``request_execution_permission()`` before the Iron Gate
        prompt, and can be called standalone for any diff preview need.
        """
        short = _short_id(op_id) if op_id else ""
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
                files_str += f" [{_C['dim']}]+{len(target_files)-1} more[/{_C['dim']}]"

        id_str = f"  [{_C['dim']}]op:{short}[/{_C['dim']}]" if short else ""
        c.print(
            f"[{_C['neural']}]📋 proposed changes[/{_C['neural']}] │{files_str}{id_str}",
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

    # ── Helpers ────────────────────────────────────────────────

    def _separator(self) -> None:
        """Full-width separator between sections."""
        width = min(self.console.width, 70)
        self.console.print(f"[{_C['dim']}]{'━' * width}[/{_C['dim']}]", highlight=False)

    def _cycle_separator(self) -> None:
        """Compact separator between operation cycles."""
        budget_str = f"💰 ${self._cost_total:.4f} of ${self._cost_cap:.2f}"
        stats = (
            f"[green]✅ {self._completed}[/green]  "
            f"[red]💀 {self._failed}[/red]"
        )
        width = min(self.console.width, 70)
        label = f" 🐍 {stats} │ {budget_str} "
        pad = width - len(label) + 30  # rough markup compensation
        half = max(2, pad // 2)
        self.console.print(
            f"[{_C['dim']}]{'━' * half}[/{_C['dim']}]"
            f"{label}"
            f"[{_C['dim']}]{'━' * half}[/{_C['dim']}]",
            highlight=False,
        )
        self.console.print()

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
                        f"[dim]⏭️  boot recovery │ {self._boot_recovery_count} stale entries reconciled[/dim]",
                        highlight=False,
                    )
                    self._flow.console.print()

                if payload.get("risk_tier") not in ("routing",):
                    # New op — clear dedup sets so this op gets fresh display
                    self._validation_shown.discard(op_id)
                    self._synthesizing_shown.discard(op_id)
                    self._flow.op_started(
                        op_id=op_id,
                        goal=payload.get("goal", ""),
                        target_files=payload.get("target_files", []),
                        risk_tier=payload.get("risk_tier", ""),
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
                        # Pre-execution: spin a masking spinner
                        self._flow.op_tool_start(
                            op_id=op_id,
                            tool_name=payload["tool_name"],
                            args_summary=payload.get("tool_args_summary", ""),
                            round_index=payload.get("round_index", 0),
                        )
                    else:
                        # Post-execution: stop spinner, print artifact
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
                    # Show candidate summary (files + rationales)
                    candidate_files = payload.get("candidate_files", [])
                    candidate_rationales = payload.get("candidate_rationales", [])
                    if candidate_files or candidate_rationales:
                        self._flow.show_code_preview(
                            op_id=op_id,
                            provider=provider,
                            candidate_files=candidate_files,
                            candidate_rationales=candidate_rationales,
                        )

                # Validation — dedup: show once per op (orchestrator emits per-candidate)
                elif phase.upper() in ("VALIDATE", "VALIDATE_RETRY") and "test_passed" in payload:
                    if op_id not in self._validation_shown:
                        self._validation_shown.add(op_id)
                        self._flow.op_validation(
                            op_id=op_id,
                            passed=payload.get("test_passed", False),
                            test_count=payload.get("test_count", 0),
                            failures=payload.get("test_failures", 0),
                        )

                # Validation phase starting — spin masking spinner until results arrive
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
                # (orchestrator emits streaming=start per retry attempt)
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

                # Proactive alert from background tasks (sensors, consciousness)
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

                # Suppress boot_recovery spam — these are stale ledger entries
                # replayed at startup, not live operations. Count and summarize.
                if reason_code.startswith("boot_recovery_"):
                    self._boot_recovery_count += 1
                    if self._boot_recovery_count == 1:
                        self._flow.console.print(
                            f"[dim]⏭️  boot recovery │ reconciling stale ledger entries...[/dim]",
                            highlight=False,
                        )
                    return

                # Escalation — emit proactive alert (similarity gate, security, etc.)
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

    Conforms to the ``ApprovalProvider`` protocol so the orchestrator
    can use it as a drop-in replacement.
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
        """Show diff + Iron Gate prompt, then route decision to inner provider.

        If the user approves, calls ``inner.approve()``.
        If the user rejects, calls ``inner.reject()``.
        The returned ``ApprovalResult`` comes from the inner provider
        so the orchestrator sees a standard result.
        """
        # Retrieve the pending request context from the inner provider
        pending = self._inner._requests.get(request_id)
        if pending is None or pending.result is not None:
            return await self._inner.await_decision(request_id, timeout_s)

        ctx = pending.context
        op_id = ctx.op_id
        description = ctx.description or ""
        target_files = list(ctx.target_files) if ctx.target_files else []

        # Generate proposed diff from the candidate.
        # The candidate lives on ctx.validation.best_candidate (ValidationResult)
        # or can be found via ctx.generation.candidates[0].
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

        # Render Iron Gate prompt
        risk_reason = getattr(ctx, "terminal_reason_code", "") or ""
        approved = await self._flow.request_execution_permission(
            op_id=op_id,
            description=description,
            target_files=target_files,
            risk_reason=risk_reason,
            diff_text=diff_text,
            candidate_rationale=candidate_rationale,
        )

        # Route decision through the inner provider
        if approved:
            return await self._inner.approve(request_id, "operator")
        else:
            return await self._inner.reject(request_id, "operator", "rejected via Iron Gate")

    async def list_pending(self) -> List[Dict[str, Any]]:
        """Delegate to inner provider."""
        return await self._inner.list_pending()


# ══════════════════════════════════════════════════════════════
# SerpentREPL — Non-blocking async REPL (prompt_toolkit)
# ══════════════════════════════════════════════════════════════


class SerpentREPL:
    """Non-blocking REPL that coexists with the Ouroboros async event loop.

    Uses ``prompt_toolkit.PromptSession.prompt_async()`` so the daemon
    can wait for human input without blocking the event loop or halting
    background telemetry, sensor polling, or streaming output.

    Parameters
    ----------
    flow:
        SerpentFlow instance — used for styled output via ``flow.console``.
    on_command:
        Async callback invoked with each line of user input.
        Signature: ``async (command: str) -> None``
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
        self._session: Any = None  # PromptSession — lazy-initialized
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        """Start the REPL loop as a background task on the current event loop."""
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
        """Async REPL loop — yields to the event loop between prompts."""
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.formatted_text import HTML
            from prompt_toolkit.patch_stdout import patch_stdout
        except ImportError:
            # prompt_toolkit not installed — degrade gracefully
            self._flow.console.print(
                f"[{_C['dim']}]REPL disabled: prompt_toolkit not installed[/{_C['dim']}]",
                highlight=False,
            )
            return

        self._session = PromptSession()

        # patch_stdout redirects plain print/logging through prompt_toolkit
        # so that background output doesn't corrupt the prompt line.
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
                            f"[{_C['dim']}]Shutting down…[/{_C['dim']}]",
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
                                f"[{_C['death']}]Error: {exc}[/{_C['death']}]",
                                highlight=False,
                            )
                except EOFError:
                    break
                except KeyboardInterrupt:
                    continue
                except asyncio.CancelledError:
                    break

    def _print_status(self) -> None:
        """Print current organism status."""
        f = self._flow
        elapsed = time.time() - f._started_at
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        f.console.print(
            f"\n[{_C['neural']}]🐍 status[/{_C['neural']}] │ "
            f"⏱ {mins}m {secs:02d}s │ "
            f"[green]✅ {f._completed} evolved[/green]  "
            f"[red]💀 {f._failed} shed[/red]  "
            f"💰 ${f._cost_total:.4f} / ${f._cost_cap:.2f}\n",
            highlight=False,
        )

    def _print_help(self) -> None:
        """Print available REPL commands."""
        self._flow.console.print(
            f"\n[{_C['neural']}]🐍 commands[/{_C['neural']}]\n"
            f"  [{_C['dim']}]status[/{_C['dim']}]   — current organism status\n"
            f"  [{_C['dim']}]help[/{_C['dim']}]     — this message\n"
            f"  [{_C['dim']}]quit[/{_C['dim']}]     — graceful shutdown\n",
            highlight=False,
        )
