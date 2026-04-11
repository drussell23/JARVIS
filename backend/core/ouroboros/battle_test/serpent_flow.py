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
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from rich.console import Console
from rich.live import Live
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


# ── Failure reason → actionable suggestion mapping ──────────────
# Each tuple: (substring to match in reason, suggestion template).
# First match wins. Templates can use {elapsed:.0f} for duration.
_FAILURE_SUGGESTIONS: list = [
    # Timeouts
    ("timed out", "Try: increase JARVIS_GENERATION_TIMEOUT_S or reduce file complexity"),
    ("timeout", "Try: increase JARVIS_GENERATION_TIMEOUT_S or reduce file complexity"),
    ("deadline", "Generation deadline exceeded. Try: split into smaller changes"),
    # Provider failures
    ("rate limit", "Provider throttled. DW will auto-recover; or set DOUBLEWORD_REALTIME_ENABLED=false"),
    ("429", "Rate-limited. The failback FSM will retry — no action needed"),
    ("503", "Provider unavailable. Failback will route to next tier automatically"),
    ("502", "Bad gateway. Transient — will retry on next sensor tick"),
    ("connection", "Network error. Check connectivity or increase JARVIS_DW_CONNECT_TIMEOUT_S"),
    # Validation / gate failures
    ("validation failed", "Patch failed structural checks. Review VALIDATE constraints or relax with /risk"),
    ("syntax error", "Generated code has syntax errors. May need simpler target or richer context"),
    ("parse error", "Output could not be parsed. Provider may need a clearer prompt — check target complexity"),
    ("no changes", "Generation produced empty diff. Signal may be stale — will be de-duplicated"),
    ("empty", "No output from provider. Retry will use fresh context"),
    # Iron Gate / approval
    ("rejected", "Human rejected at Iron Gate. Constraint recorded — organism will avoid this pattern"),
    ("blocked", "Risk tier BLOCKED. Requires /risk notify_apply or JARVIS_DEFAULT_RISK_TIER=NOTIFY_APPLY"),
    ("approval", "Needs human approval. Use /risk safe_auto for auto-approve or respond in REPL"),
    # Repair failures
    ("repair failed", "L2 repair exhausted 5 iterations. Manual intervention needed on this file"),
    ("repair timeout", "L2 repair timed out (120s). Try: reduce repair scope or increase JARVIS_REPAIR_TIMEOUT_S"),
    # Test failures
    ("test fail", "Post-apply tests failed. L2 repair will attempt fix; if persistent, check test fixtures"),
    ("pytest", "Test suite error. Check for missing fixtures or flaky tests"),
    # Stale / conflict
    ("stale", "Files changed since generation started. Fresh context will be used on retry"),
    ("conflict", "Merge conflict on apply. Another operation may have touched the same files"),
    ("lock", "File lock held by another operation. Will retry after lock TTL expires"),
    # Cost
    ("cost cap", "Session budget exhausted. Increase --cost-cap or set OUROBOROS_BATTLE_COST_CAP"),
    ("budget", "Budget limit reached. Use /budget <amount> to adjust mid-session"),
    # Catch-all handled below
]


def _actionable_suggestion(reason: str, phase: str, elapsed: float) -> str:
    """Map a failure reason to a concrete next-step suggestion."""
    reason_lower = reason.lower()
    for pattern, suggestion in _FAILURE_SUGGESTIONS:
        if pattern in reason_lower:
            return suggestion

    # Phase-specific fallbacks
    if phase:
        phase_lower = phase.lower()
        if "generate" in phase_lower:
            return f"Generation failed after {elapsed:.0f}s. Check provider logs or try a simpler target"
        if "validate" in phase_lower:
            return "Validation rejected the patch. Review constraints in VALIDATE phase config"
        if "apply" in phase_lower:
            return "Apply failed. Check file permissions and git working tree state"
        if "verify" in phase_lower:
            return "Post-apply verification failed. L2 repair will handle if enabled"

    return f"Failed after {elapsed:.0f}s. Check debug.log for details: grep {reason[:20]!r}"


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


def _parse_unified_diff(diff_text: str) -> tuple:
    """Parse a unified diff into (added, removed, hunks).

    Returns
    -------
    added : int
        Total lines added across all hunks.
    removed : int
        Total lines removed across all hunks.
    hunks : list of dict
        Each dict has ``old_start``, ``new_start``, and ``lines``
        (raw diff lines including the +/-/space prefix).
    """
    added = 0
    removed = 0
    hunks: List[Dict[str, Any]] = []
    current_hunk: Optional[Dict[str, Any]] = None

    _HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for line in diff_text.split("\n"):
        # Skip file headers
        if line.startswith("diff ") or line.startswith("index "):
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            continue

        hunk_match = _HUNK_RE.match(line)
        if hunk_match:
            current_hunk = {
                "old_start": int(hunk_match.group(1)),
                "new_start": int(hunk_match.group(2)),
                "lines": [],
            }
            hunks.append(current_hunk)
            continue

        if current_hunk is not None:
            current_hunk["lines"].append(line)
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1

    return added, removed, hunks


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
        # Per-op reasoning — captured at GENERATE, shown at ⏺ Update
        self._op_rationales: Dict[str, str] = {}

        # Rich console — force_terminal=True ensures ANSI codes survive
        # prompt_toolkit's patch_stdout proxy (which replaces sys.stdout
        # with a non-tty wrapper). Without this, Rich detects the proxy
        # as non-terminal and falls back to plain text.
        self.console = Console(emoji=True, highlight=False, force_terminal=True)

        # Execution masking (rich.Status)
        self._active_status: Optional[Status] = None

        # Live syntax-highlighted streaming (rich.Live + rich.Syntax)
        self._live: Optional[Live] = None
        self._stream_buffer: str = ""
        self._stream_language: str = "json"

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
    # Live syntax-highlighted streaming (rich.Live + rich.Syntax)
    # ══════════════════════════════════════════════════════════

    def show_streaming_start(
        self, provider: str, op_id: str = "", language: str = "",
    ) -> None:
        """Begin synthesis — header + open Live Syntax panel.

        P3.2: Uses Rich.Syntax for real-time syntax highlighting
        during DW SSE token streaming, matching CC's highlighting UX.
        """
        self._streaming_active = True
        self._stream_buffer = ""

        # Detect language from target files if available
        if not language:
            language = "json"  # Model output is JSON by default

        self._stream_language = language

        prov = _prov(provider) if provider else ""
        via_str = f" via [{_C['provider']}]{prov}[/{_C['provider']}]" if prov else ""
        self._op_line(
            op_id,
            f"[{_C['neural']}]🧬 synthesizing[/{_C['neural']}] {via_str}",
        )

        # Open Live Syntax rendering.  transient=False keeps output
        # in terminal history after the stream ends.
        self._live = Live(
            Syntax("", language, theme="monokai", word_wrap=True),
            console=self.console,
            transient=False,
            refresh_per_second=8,
        )
        self._live.start()

    def show_streaming_token(self, token: str) -> None:
        """Append a token and re-render the Syntax panel in-place.

        P3.2: Syntax highlighting on every update — Rich.Syntax lexes
        the accumulated buffer, giving real-time code coloring as
        tokens arrive from the SSE stream.
        """
        if not token:
            return
        self._stream_buffer += token
        if self._live is not None:
            try:
                lang = getattr(self, "_stream_language", "json")
                self._live.update(
                    Syntax(
                        self._stream_buffer, lang,
                        theme="monokai", word_wrap=True,
                    )
                )
            except Exception:
                pass

    def show_streaming_end(self) -> None:
        """Finalize the Live Syntax region."""
        if self._live is not None:
            try:
                if self._stream_buffer:
                    lang = getattr(self, "_stream_language", "json")
                    self._live.update(
                        Syntax(
                            self._stream_buffer, lang,
                            theme="monokai", word_wrap=True,
                        )
                    )
                self._live.stop()
            except Exception:
                pass
            self._live = None
        self._stream_buffer = ""
        self._stream_language = "json"
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

    def op_phase(
        self, op_id: str, phase: str, progress_pct: float = 0.0,
        **kwargs: Any,
    ) -> None:
        """Phase transition — only log significant phases."""
        phase_upper = phase.upper()
        if phase_upper in ("CLASSIFY", "ROUTE", "CONTEXT_EXPANSION", "GENERATE", "VALIDATE"):
            return  # Handled by dedicated methods
        if phase_upper == "PLAN":
            self._render_plan_phase(op_id, **kwargs)
            return
        if phase_upper == "COMMIT":
            self._render_commit_phase(op_id, **kwargs)
            return
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

    def _render_plan_phase(self, op_id: str, **kwargs: Any) -> None:
        """Render the PLAN phase with complexity and change count."""
        complexity = kwargs.get("plan_complexity", "")
        n_changes = kwargs.get("plan_changes", 0)
        if complexity:
            # Plan result — show complexity + change count
            color = {
                "trivial": _C["dim"],
                "moderate": _C["neural"],
                "complex": _C["heal"],
                "architectural": _C["provider"],
            }.get(complexity, _C["neural"])
            detail = f"[{color}]{complexity}[/{color}]"
            if n_changes:
                detail += f"  [{_C['dim']}]{n_changes} ordered changes[/{_C['dim']}]"
            self._op_line(
                op_id,
                f"[{_C['neural']}]🗺️  planned[/{_C['neural']}]   {detail}",
            )
        else:
            # Plan phase starting
            self._op_line(
                op_id,
                f"[{_C['neural']}]🗺️  planning[/{_C['neural']}]  "
                f"[{_C['dim']}]reasoning about implementation strategy...[/{_C['dim']}]",
            )

    def _render_commit_phase(self, op_id: str, **kwargs: Any) -> None:
        """Render the auto-commit result with hash and push status."""
        commit_hash = kwargs.get("commit_hash", "")
        pushed = kwargs.get("commit_pushed", False)
        branch = kwargs.get("commit_branch", "")
        if commit_hash:
            parts = f"[{_C['life']}]{commit_hash}[/{_C['life']}]"
            if pushed and branch:
                parts += f"  [{_C['dim']}]-> {branch}[/{_C['dim']}]"
            self._op_line(
                op_id,
                f"[{_C['life']}]📝 committed[/{_C['life']}]  {parts}  "
                f"[{_C['dim']}]O+V[/{_C['dim']}]",
            )

    # ── Intent Chain (P3.1: full reasoning chain visibility) ──

    def update_intent_chain(
        self, op_id: str, risk_tier: str = "", complexity: str = "",
        auto_approve: bool = False, fast_path: bool = False,
        sensor: str = "",
    ) -> None:
        """Render the full reasoning chain in a single compact line.

        Shows: sensor → complexity → risk → routing path.
        Manifesto §7: Absolute observability — every autonomous decision visible.
        """
        parts: List[str] = []

        # Sensor origin
        if sensor:
            parts.append(f"[{_C['dim']}]{sensor}[/{_C['dim']}]")

        # Complexity badge
        if complexity:
            cx_color = {
                "trivial": _C["dim"],
                "light": _C["neural"],
                "moderate": _C["neural"],
                "heavy_code": _C["heal"],
                "complex": _C["provider"],
            }.get(complexity, _C["dim"])
            parts.append(f"[{cx_color}]{complexity}[/{cx_color}]")

        # Risk tier badge
        if risk_tier:
            rt = risk_tier.upper()
            if rt in ("SAFE_AUTO", "LOW"):
                rt_color = "green"
            elif rt in ("NOTIFY_APPLY", "MEDIUM"):
                rt_color = _C["heal"]
            else:
                rt_color = _C["death"]
            parts.append(f"[{rt_color}]{rt}[/{rt_color}]")

        # Routing path hint
        if fast_path:
            parts.append(f"[{_C['dim']}]fast-path[/{_C['dim']}]")
        elif auto_approve:
            parts.append(f"[{_C['dim']}]auto-approve[/{_C['dim']}]")

        if not parts:
            return

        chain = f" [{_C['dim']}]→[/{_C['dim']}] ".join(parts)
        self._op_line(
            op_id,
            f"[{_C['neural']}]🔗 chain[/{_C['neural']}]     {chain}",
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
        cost_usd: float = 0.0,
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

        # Per-operation cost (3 decimal places for sub-cent, 2 for larger)
        if cost_usd >= 0.01:
            cost_str = f" [{_C['dim']}]│[/{_C['dim']}] ${cost_usd:.2f}"
        elif cost_usd > 0.001:
            cost_str = f" [{_C['dim']}]│[/{_C['dim']}] ${cost_usd:.3f}"
        else:
            cost_str = ""

        self._op_line(
            op_id,
            f"[{_C['neural']}]🧬 synthesized[/{_C['neural']}]  "
            f"{candidates} candidate{'s' if candidates != 1 else ''} via "
            f"[{_C['provider']}]{model_str}[/{_C['provider']}]"
            f"{tools_str}{token_str}{cost_str}"
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
        """Venom tool call completed — stop spinner, print artifact.

        Write tools (edit_file, write_file) render CC-style
        ``⏺ Update(path)`` / ``⏺ Write(path)`` blocks.
        Read tools show a compact one-liner.
        """
        self._stop_status()

        tool_icons = {
            "read_file": "📄", "search_code": "🔍", "run_tests": "🧪",
            "bash": "💻", "web_search": "🌐", "web_fetch": "🌐",
            "get_callers": "🔗", "list_symbols": "📋",
            "glob_files": "📁", "list_dir": "📂",
            "git_log": "📜", "git_diff": "📊", "git_blame": "🔎",
            "edit_file": "✏️", "write_file": "📝",
            "code_explore": "🧪",
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

        # ── CC-style blocks for write/edit tools ──
        if tool_name == "edit_file" and status == "success":
            # Render as ⏺ Update(path) with result preview as inline diff
            path = args_summary[:60] if args_summary else "file"
            self._op_line(
                op_id,
                f"[{_C['neural']}]⏺ Update[/{_C['neural']}]"
                f"([{_C['file']}]{path}[/{_C['file']}]){dur}",
            )
            if result_preview:
                # Count added/removed lines from result
                lines = result_preview.split("\n")
                n_changed = sum(1 for l in lines if l.strip())
                self._op_line(
                    op_id,
                    f"[{_C['dim']}]⎿  edit applied ({n_changed} line{'s' if n_changed != 1 else ''} affected)[/{_C['dim']}]",
                )
            return

        if tool_name == "write_file" and status == "success":
            path = args_summary[:60] if args_summary else "file"
            self._op_line(
                op_id,
                f"[{_C['neural']}]⏺ Write[/{_C['neural']}]"
                f"([{_C['file']}]{path}[/{_C['file']}]){dur}",
            )
            if result_preview:
                n_lines = result_preview.count("\n") + 1
                self._op_line(
                    op_id,
                    f"[{_C['dim']}]⎿  {n_lines} line{'s' if n_lines != 1 else ''} written[/{_C['dim']}]",
                )
            return

        # ── Read tool: CC-style Read(path) header ──
        if tool_name == "read_file":
            path = args_summary[:60] if args_summary else "file"
            self._op_line(
                op_id,
                f"[{_C['neural']}]⏺ Read[/{_C['neural']}]"
                f"([{_C['file']}]{path}[/{_C['file']}]){dur}{status_mark}",
            )
            return

        # ── Default: compact one-liner for other tools ──
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

    # ── Post-apply Verify ─────────────────────────────────────

    def op_verify_start(self, op_id: str, target_files: Optional[List[str]] = None) -> None:
        """Spin a masking spinner while post-apply verification runs."""
        files = target_files or []
        files_str = ", ".join(f.split("/")[-1] for f in files[:3])
        if len(files) > 3:
            files_str += f" +{len(files) - 3}"
        prefix = "  │  " if op_id in self._active_ops else "  "
        self._start_status(
            f"{prefix}⏺ Verify({files_str})",
            spinner="dots",
        )

    def op_verify_result(
        self, op_id: str, passed: bool,
        test_total: int = 0, test_failures: int = 0,
        target_files: Optional[List[str]] = None,
    ) -> None:
        """Post-apply verify result — CC-style ⏺ Verify block."""
        self._stop_status()
        files = target_files or []
        files_str = ", ".join(f.split("/")[-1] for f in files[:3])
        if len(files) > 3:
            files_str += f" +{len(files) - 3}"

        if test_total == 0:
            self._op_line(
                op_id,
                f"[{_C['heal']}]⏺ Verify[/{_C['heal']}]({files_str})",
            )
            self._op_line(
                op_id,
                f"[{_C['dim']}]⎿  no scoped tests found[/{_C['dim']}]",
            )
        elif passed:
            self._op_line(
                op_id,
                f"[{_C['life']}]⏺ Verify[/{_C['life']}]({files_str})",
            )
            self._op_line(
                op_id,
                f"  [{_C['dim']}]⎿[/{_C['dim']}]  [green]✅ {test_total}/{test_total} passing[/green]",
            )
        else:
            passing = test_total - test_failures
            self._op_line(
                op_id,
                f"[{_C['death']}]⏺ Verify[/{_C['death']}]({files_str})",
            )
            self._op_line(
                op_id,
                f"  [{_C['dim']}]⎿[/{_C['dim']}]  [red]❌ {test_failures} failing, {passing} passing[/red]",
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

    def set_op_reasoning(self, op_id: str, reasoning: str) -> None:
        """Store per-op reasoning for display in ⏺ Update blocks."""
        if reasoning:
            self._op_rationales[op_id] = reasoning.strip()

    def show_diff(
        self, file_path: str, diff_text: str = "", op_id: str = "",
        reasoning: str = "",
    ) -> None:
        """Show a CC-style inline update block for a file change.

        Renders the Claude Code ``⏺ Update(path)`` pattern with summary
        counts, numbered context lines, and colored +/- diff markers.
        Falls back to a compact one-liner when no diff is available.
        """
        if not diff_text:
            diff_text = self._get_git_diff(file_path)

        short_path = file_path
        if len(file_path) > 60:
            parts = file_path.split("/")
            short_path = "/".join(parts[-3:]) if len(parts) >= 3 else file_path

        if not diff_text:
            self._op_line(
                op_id,
                f"[{_C['neural']}]⏺ Update[/{_C['neural']}]"
                f"([{_C['file']}]{short_path}[/{_C['file']}])",
            )
            return

        # Parse unified diff into structured hunks
        added, removed, hunks = _parse_unified_diff(diff_text)

        # ── Header: ⏺ Update(path) ──
        self._op_line(
            op_id,
            f"[{_C['neural']}]⏺ Update[/{_C['neural']}]"
            f"([{_C['file']}]{short_path}[/{_C['file']}])",
        )

        # ── Summary: ⎿  Added N lines, removed M lines ──
        parts: List[str] = []
        if added:
            parts.append(f"[{_C['code_add']}]Added {added} line{'s' if added != 1 else ''}[/{_C['code_add']}]")
        if removed:
            parts.append(f"[{_C['code_del']}]removed {removed} line{'s' if removed != 1 else ''}[/{_C['code_del']}]")
        summary = ", ".join(parts) if parts else "no changes"
        self._op_line(op_id, f"[{_C['dim']}]⎿[/{_C['dim']}]  {summary}")

        # ── Reasoning: why the organism made this change ──
        # Check explicit parameter first, then stored per-op reasoning
        _reason = reasoning or self._op_rationales.get(op_id, "")
        if _reason:
            # Escape markup in model-generated text
            safe_reason = _reason.replace("[", "\\[")[:120]
            self._op_line(
                op_id,
                f"[{_C['dim']}]⎿  reasoning: {safe_reason}[/{_C['dim']}]",
            )

        # ── Contextual diff lines (max 3 hunks, 20 lines each) ──
        hunk_limit = 3
        lines_per_hunk = 20
        for hunk_idx, hunk in enumerate(hunks[:hunk_limit]):
            old_start = hunk["old_start"]
            new_start = hunk["new_start"]
            old_lineno = old_start
            new_lineno = new_start

            shown = 0
            for diff_line in hunk["lines"][:lines_per_hunk]:
                kind = diff_line[0] if diff_line else " "
                content = diff_line[1:] if len(diff_line) > 1 else ""
                # Escape Rich markup in code content
                safe = content.replace("[", "\\[")

                if kind == "-":
                    self._op_line(
                        op_id,
                        f"    [{_C['dim']}]{old_lineno:>5}[/{_C['dim']}] "
                        f"[{_C['code_del']}]- {safe}[/{_C['code_del']}]",
                    )
                    old_lineno += 1
                elif kind == "+":
                    self._op_line(
                        op_id,
                        f"    [{_C['dim']}]{new_lineno:>5}[/{_C['dim']}] "
                        f"[{_C['code_add']}]+ {safe}[/{_C['code_add']}]",
                    )
                    new_lineno += 1
                else:
                    # Context line
                    self._op_line(
                        op_id,
                        f"    [{_C['dim']}]{new_lineno:>5}   {safe}[/{_C['dim']}]",
                    )
                    old_lineno += 1
                    new_lineno += 1
                shown += 1

            remaining_in_hunk = len(hunk["lines"]) - shown
            if remaining_in_hunk > 0:
                self._op_line(
                    op_id,
                    f"    [{_C['dim']}]      ... +{remaining_in_hunk} lines[/{_C['dim']}]",
                )

        remaining_hunks = len(hunks) - hunk_limit
        if remaining_hunks > 0:
            self._op_line(
                op_id,
                f"    [{_C['dim']}]      ... +{remaining_hunks} more hunk{'s' if remaining_hunks != 1 else ''}[/{_C['dim']}]",
            )

    def show_diff_preview(
        self,
        diff_text: str,
        target_files: Optional[List[str]] = None,
        op_id: str = "",
    ) -> None:
        """Render a CC-style diff preview for the approval flow.

        Uses the same ``⏺ Update(path)`` layout as ``show_diff`` but
        renders per-file blocks for each target file in the diff.
        """
        if not target_files:
            target_files = []

        # Parse the full diff to get per-file counts
        added, removed, hunks = _parse_unified_diff(diff_text)

        for tf in target_files:
            short = tf
            if len(tf) > 60:
                parts = tf.split("/")
                short = "/".join(parts[-3:]) if len(parts) >= 3 else tf
            # Show each file with its own update block
            self.show_diff(tf, diff_text=diff_text, op_id=op_id)

        # If no target files provided, show a standalone summary
        if not target_files and diff_text:
            parts_sum: List[str] = []
            if added:
                parts_sum.append(f"[{_C['code_add']}]+{added}[/{_C['code_add']}]")
            if removed:
                parts_sum.append(f"[{_C['code_del']}]-{removed}[/{_C['code_del']}]")
            summary = " ".join(parts_sum) if parts_sum else "no changes"
            self._op_line(
                op_id,
                f"[{_C['neural']}]⏺ Proposed changes[/{_C['neural']}]  {summary}",
            )

    # ── Operation completion ──────────────────────────────────

    def op_completed(
        self, op_id: str, files_changed: List[str],
        provider: str = "", cost_usd: float = 0.0,
        reasoning: str = "",
    ) -> None:
        """The organism evolved — operation succeeded."""
        self._stop_status()
        self._completed += 1
        elapsed = time.time() - self._op_starts.pop(op_id, time.time())
        prov = _prov(self._op_providers.pop(op_id, provider))
        self._cost_total += cost_usd

        # Store reasoning for display in ⏺ Update blocks
        if reasoning:
            self._op_rationales[op_id] = reasoning

        # Show diffs as CC-style ⏺ Update blocks (reasoning shown inline)
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

        # Close the op block and clean up per-op state
        self._op_rationales.pop(op_id, None)
        self._close_op_block(op_id)

    def op_failed(self, op_id: str, reason: str, phase: str = "") -> None:
        """The organism shed a failed change."""
        self._stop_status()
        self._failed += 1
        elapsed = time.time() - self._op_starts.pop(op_id, time.time())
        self._op_providers.pop(op_id, None)
        self._op_rationales.pop(op_id, None)

        phase_str = f" at [{_C['neural']}]{phase}[/{_C['neural']}]" if phase else ""

        self._op_line(
            op_id,
            f"[{_C['death']}]💀 shed[/{_C['death']}]        "
            f"[{_C['death']}]{reason[:70]}[/{_C['death']}]{phase_str}"
            f"  [{_C['dim']}]⏱ {elapsed:.1f}s[/{_C['dim']}]",
        )

        # Actionable next-step based on failure reason
        suggestion = _actionable_suggestion(reason, phase, elapsed)
        self._op_line(
            op_id,
            f"[{_C['dim']}]             💡 {suggestion}[/{_C['dim']}]",
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
            with patch_stdout(raw=True):
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
    # Plan Approval Hard Gate (Phase 1b)
    # ══════════════════════════════════════════════════════════

    async def request_plan_permission(
        self,
        op_id: str,
        description: str,
        target_files: List[str],
        plan_text: str,
        complexity: str = "",
    ) -> bool:
        """Interactive [Y/n] gate for *implementation plans* (pre-GENERATE).

        Identical interaction model to :meth:`request_execution_permission`,
        but renders the model-generated plan as markdown instead of a code
        diff. Used by the Plan Approval Hard Gate (Manifesto §6) for
        COMPLEX/ARCHITECTURAL ops — the human sees the approach before
        any tokens are burned on code generation.

        Returns ``True`` for approval, ``False`` for rejection.
        """
        short = _short_id(op_id) if op_id else ""
        c = self.console

        # Step 1: Render plan as markdown
        try:
            from rich.markdown import Markdown
            from rich.panel import Panel as _Panel

            plan_panel = _Panel(
                Markdown(plan_text or "_(no plan content)_"),
                title=(
                    f"📝 Implementation Plan │ "
                    f"{complexity or 'unclassified'} │ op:{short}"
                ),
                border_style=_C["mind"],
                expand=False,
                width=min(c.width, 90),
                padding=(0, 1),
            )
            c.print()
            c.print(plan_panel)
        except Exception:
            # Markdown rendering failed — fall back to plain text
            c.print()
            c.print(
                f"[{_C['mind']}]📝 Implementation Plan │ op:{short}[/{_C['mind']}]"
            )
            c.print(plan_text or "(no plan content)")

        # Step 2: Plan Gate panel
        body_lines = [f"[bold]{description}[/bold]"]
        if target_files:
            files_display = ", ".join(
                f.split("/")[-1] if "/" in f else f for f in target_files[:5]
            )
            body_lines.append(f"📂 {files_display}")
        body_lines.append(
            f"[{_C['dim']}]Approve the APPROACH before code is generated. "
            f"Rejection prevents wasted tokens on a wrong strategy.[/{_C['dim']}]"
        )

        panel = Panel(
            "\n".join(body_lines),
            title=f"🔒 Plan Gate │ op:{short}",
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
            with patch_stdout(raw=True):
                answer = await session.prompt_async(
                    HTML("<b>  Approve this plan and proceed to GENERATE? [Y/n] </b>"),
                )
            answer = answer.strip().lower()
            approved = answer in ("", "y", "yes")
        except ImportError:
            c.print(
                f"  [{_C['heal']}](prompt_toolkit unavailable — auto-approving plan)[/{_C['heal']}]",
                highlight=False,
            )
            approved = True
        except (EOFError, KeyboardInterrupt):
            approved = False

        # Step 4: Decision artifact
        if approved:
            c.print(
                f"  [{_C['life']}]✅ plan approved[/{_C['life']}]  [{_C['dim']}]op:{short}[/{_C['dim']}]",
                highlight=False,
            )
        else:
            c.print(
                f"  [{_C['death']}]❌ plan rejected[/{_C['death']}]  [{_C['dim']}]op:{short}[/{_C['dim']}]",
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


    # ══════════════════════════════════════════════════════════
    # ExecutionGraph rendering (Phase 3b multi-op visibility)
    # ══════════════════════════════════════════════════════════

    def render_execution_graph(
        self,
        progress: Any,
        *,
        op_id: str = "",
        show_critical_path: bool = True,
        max_units_rendered: int = 12,
    ) -> None:
        """Render a live multi-op graph progress view.

        Displays a compact summary of an ``ExecutionGraph`` as tracked
        by ``ExecutionGraphProgressTracker``: header with graph id /
        phase / completion ratio, per-unit lanes with status markers
        and timing, the current critical path highlight, and any
        recorded merge decisions.

        Parameters
        ----------
        progress:
            A ``GraphProgress`` snapshot. Typed as ``Any`` so this
            module doesn't pull the autonomy package as a hard import
            (SerpentFlow should still work if L3 is disabled).
        op_id:
            Parent operation id — used to indent the rendering inside
            an active op block so the graph belongs to the op visually.
        show_critical_path:
            Whether to compute and highlight the DAG critical path.
        max_units_rendered:
            Cap on per-unit lane rows (large graphs get a "...N more"
            footer instead of pages of output).
        """
        if progress is None:
            return

        # Header — graph id, phase, completion %.
        pct = int(round(progress.completion_pct() * 100))
        phase_value = getattr(progress.phase, "value", str(progress.phase))
        phase_color = {
            "created": _C["dim"],
            "running": _C["neural"],
            "completed": _C["life"],
            "failed": _C["death"],
            "cancelled": _C["heal"],
        }.get(phase_value, _C["dim"])

        header = (
            f"[{_C['neural']}]⏺ Graph[/{_C['neural']}]"
            f"([{_C['file']}]{progress.graph_id[:12]}[/{_C['file']}])"
            f"  [{phase_color}]{phase_value}[/{phase_color}]"
            f"  [{_C['dim']}]{pct}% done"
            f"  {len(progress.units)} units[/{_C['dim']}]"
        )
        self._op_line(op_id, header)

        if progress.runtime_ms > 0:
            runtime_str = (
                f"{progress.runtime_ms:.0f}ms"
                if progress.runtime_ms < 1000
                else f"{progress.runtime_ms / 1000:.1f}s"
            )
            self._op_line(
                op_id,
                f"[{_C['dim']}]⎿  runtime: {runtime_str}  "
                f"concurrency: {progress.concurrency_limit}[/{_C['dim']}]",
            )

        # Critical path highlight.
        critical_set: set = set()
        if show_critical_path:
            try:
                critical_set = set(progress.critical_path())
            except Exception:
                critical_set = set()
            if critical_set:
                chain_repr = " → ".join(
                    f"[{_C['heal']}]{uid}[/{_C['heal']}]" if uid in critical_set else uid
                    for uid in list(critical_set)[:6]
                )
                self._op_line(
                    op_id,
                    f"[{_C['dim']}]⎿  critical path: {chain_repr}[/{_C['dim']}]",
                )

        # Per-unit lanes.
        unit_status_glyph = {
            "pending": ("○", _C["dim"]),
            "running": ("◐", _C["neural"]),
            "completed": ("●", _C["life"]),
            "failed": ("✗", _C["death"]),
            "cancelled": ("◌", _C["heal"]),
        }

        rendered = 0
        for unit_id, unit in progress.units.items():
            if rendered >= max_units_rendered:
                break
            state_value = getattr(unit.state, "value", str(unit.state))
            glyph, color = unit_status_glyph.get(state_value, ("?", _C["dim"]))
            is_critical = unit_id in critical_set

            # Timing: ms when running, runtime_ms when terminal.
            if state_value in ("completed", "failed", "cancelled"):
                ms = getattr(unit, "runtime_ms", 0.0)
            else:
                ms = getattr(unit, "elapsed_ms", 0.0)
            timing = ""
            if ms > 0:
                timing = (
                    f"  [{_C['dim']}]{ms:.0f}ms[/{_C['dim']}]"
                    if ms < 1000
                    else f"  [{_C['dim']}]{ms / 1000:.1f}s[/{_C['dim']}]"
                )

            target_repr = ""
            if unit.target_files:
                first = unit.target_files[0]
                if len(first) > 48:
                    parts = first.split("/")
                    first = "/".join(parts[-3:]) if len(parts) >= 3 else first
                target_repr = f"  [{_C['file']}]{first}[/{_C['file']}]"
                if len(unit.target_files) > 1:
                    target_repr += (
                        f"  [{_C['dim']}](+{len(unit.target_files) - 1})[/{_C['dim']}]"
                    )

            crit_marker = "★ " if is_critical else "  "
            lane = (
                f"  {crit_marker}[{color}]{glyph}[/{color}] "
                f"{unit_id:<14}{target_repr}{timing}"
            )
            self._op_line(op_id, lane)

            # Failure detail for failed units — single line.
            if state_value == "failed" and getattr(unit, "error", ""):
                err = unit.error.replace("[", "\\[")[:80]
                self._op_line(
                    op_id,
                    f"       [{_C['death']}]⎿ {err}[/{_C['death']}]",
                )
            rendered += 1

        overflow = len(progress.units) - rendered
        if overflow > 0:
            self._op_line(
                op_id,
                f"  [{_C['dim']}]... +{overflow} more unit"
                f"{'s' if overflow != 1 else ''}[/{_C['dim']}]",
            )

        # Merge decisions.
        decisions = getattr(progress, "merge_decisions", [])
        if decisions:
            for decision in decisions[-3:]:  # last 3 barriers
                barrier = decision.get("barrier_id", "?")
                repo = decision.get("repo", "?")
                merged = decision.get("merged_unit_ids", [])
                conflict = decision.get("conflict_units", [])
                conflict_note = (
                    f"  [{_C['death']}]{len(conflict)} conflict"
                    f"{'s' if len(conflict) != 1 else ''}[/{_C['death']}]"
                    if conflict
                    else ""
                )
                self._op_line(
                    op_id,
                    f"  [{_C['provider']}]⚭ merge[/{_C['provider']}]"
                    f"  [{_C['dim']}]{repo}:{barrier}  "
                    f"{len(merged)} units merged{conflict_note}[/{_C['dim']}]",
                )

    def render_graph_event(self, event: Any, op_id: str = "") -> None:
        """Render a single ``GraphEvent`` as a compact status line.

        Used when consuming the progress tracker's subscribe()
        iterator — gives the operator a ticker of graph activity
        without re-rendering the full multi-lane view each time.
        """
        if event is None:
            return
        kind_value = getattr(event.kind, "value", str(event.kind))
        glyphs = {
            "graph.submitted": ("⏺", _C["dim"], "submitted"),
            "graph.started": ("⏵", _C["neural"], "started"),
            "graph.completed": ("✔", _C["life"], "completed"),
            "graph.failed": ("✗", _C["death"], "failed"),
            "graph.cancelled": ("◌", _C["heal"], "cancelled"),
            "unit.ready": ("◎", _C["dim"], "ready"),
            "unit.started": ("◐", _C["neural"], "started"),
            "unit.completed": ("●", _C["life"], "completed"),
            "unit.failed": ("✗", _C["death"], "failed"),
            "unit.cancelled": ("◌", _C["heal"], "cancelled"),
            "merge.decided": ("⚭", _C["provider"], "merged"),
        }
        glyph, color, label = glyphs.get(kind_value, ("·", _C["dim"], kind_value))
        target = event.unit_id or event.graph_id[:10]
        payload = event.payload or {}
        extra = ""
        runtime_ms = payload.get("runtime_ms")
        if isinstance(runtime_ms, (int, float)) and runtime_ms > 0:
            extra = (
                f"  [{_C['dim']}]{runtime_ms:.0f}ms[/{_C['dim']}]"
                if runtime_ms < 1000
                else f"  [{_C['dim']}]{runtime_ms / 1000:.1f}s[/{_C['dim']}]"
            )
        self._op_line(
            op_id,
            f"  [{color}]{glyph}[/{color}] {target:<16} "
            f"[{_C['dim']}]{label}[/{_C['dim']}]{extra}",
        )


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

                # P3.1: Intent chain — full reasoning chain visibility
                if phase == "intent_chain":
                    sensor = self._flow._op_sensors.get(op_id, "")
                    self._flow.update_intent_chain(
                        op_id=op_id,
                        risk_tier=payload.get("risk_tier", ""),
                        complexity=payload.get("complexity", ""),
                        auto_approve=payload.get("auto_approve", False),
                        fast_path=payload.get("fast_path", False),
                        sensor=sensor,
                    )

                # Triage decision
                elif phase == "semantic_triage" and payload.get("triage_decision"):
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
                        cost_usd=payload.get("cost_usd", 0.0),
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
                    # Capture rationale for display in ⏺ Update blocks
                    if candidate_rationales:
                        self._flow.set_op_reasoning(
                            op_id, candidate_rationales[0],
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

                # Post-apply VERIFY — scoped test run
                elif phase.upper() == "VERIFY" and payload.get("verify_test_starting"):
                    self._flow.op_verify_start(
                        op_id=op_id,
                        target_files=payload.get("verify_target_files", []),
                    )
                elif phase.upper() == "VERIFY" and "verify_test_passed" in payload:
                    self._flow.op_verify_result(
                        op_id=op_id,
                        passed=payload.get("verify_test_passed", False),
                        test_total=payload.get("verify_test_total", 0),
                        test_failures=payload.get("verify_test_failures", 0),
                        target_files=payload.get("verify_target_files", []),
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
                        # P3.1: Show provider routing before streaming starts
                        self._flow.op_provider(op_id, provider)
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
                        plan_complexity=payload.get("plan_complexity", ""),
                        plan_changes=payload.get("plan_changes", 0),
                        commit_hash=payload.get("commit_hash", ""),
                        commit_pushed=payload.get("commit_pushed", False),
                        commit_branch=payload.get("commit_branch", ""),
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

                # NOTIFY_APPLY (Yellow) — auto-apply with prominent CLI notice
                if outcome == "notify_apply":
                    _files = payload.get("target_files", [])
                    _files_str = ", ".join(f[:40] for f in _files[:3])
                    if len(_files) > 3:
                        _files_str += f" +{len(_files) - 3}"
                    self._flow._op_line(
                        op_id,
                        f"[{_C['heal']}]⚠ NOTIFY[/{_C['heal']}]     "
                        f"[{_C['dim']}]{reason_code}[/{_C['dim']}]  "
                        f"[{_C['file']}]{_files_str}[/{_C['file']}]",
                    )
                    self._flow._op_line(
                        op_id,
                        f"[{_C['dim']}]⎿  auto-applying (Yellow severity — review in git log)[/{_C['dim']}]",
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

    async def request_plan(self, context: Any, plan_text: str) -> str:
        """Delegate plan-variant request registration to the inner provider.

        Part of the Plan Approval Hard Gate (Phase 1b). The inner provider
        stores ``plan_text`` on the pending request; :meth:`await_decision`
        detects it and renders the plan markdown instead of a diff.
        """
        if not hasattr(self._inner, "request_plan"):
            # Duck-type fallback: inner provider doesn't support plan
            # approval. The caller must handle this gracefully — typically
            # by skipping the plan gate entirely.
            raise NotImplementedError(
                "inner approval provider does not support request_plan"
            )
        return await self._inner.request_plan(context, plan_text)

    async def await_decision(
        self, request_id: str, timeout_s: float,
    ) -> Any:
        """Show diff/plan + Iron Gate prompt, then route decision to inner provider."""
        pending = self._inner._requests.get(request_id)
        if pending is None or pending.result is not None:
            return await self._inner.await_decision(request_id, timeout_s)

        ctx = pending.context
        op_id = ctx.op_id
        description = ctx.description or ""
        target_files = list(ctx.target_files) if ctx.target_files else []

        # ── Plan Approval Hard Gate branch ──
        # If plan_text is set, this is a pre-GENERATE plan approval request.
        # Render the plan markdown via request_plan_permission instead of
        # the code-diff flow below.
        _plan_text = getattr(pending, "plan_text", None)
        if _plan_text is not None:
            _complexity = getattr(ctx, "task_complexity", "") or ""
            approved = await self._flow.request_plan_permission(
                op_id=op_id,
                description=description,
                target_files=target_files,
                plan_text=_plan_text,
                complexity=_complexity,
            )
            if approved:
                return await self._inner.approve(request_id, "operator")
            return await self._inner.reject(
                request_id, "operator", "plan rejected via Plan Gate"
            )

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
        gls: Any = None,
    ) -> None:
        self._flow = flow
        self._on_command = on_command
        self._prompt_str = prompt_str
        self._session: Any = None
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None
        self._gls = gls  # GovernedLoopService reference for /cancel

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

        with patch_stdout(raw=True):
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
                    if line.startswith("cancel "):
                        _cancel_target = line.split(None, 1)[1].strip()
                        await self._handle_cancel(_cancel_target)
                        continue

                    # Runtime configuration commands
                    if line.startswith("/risk") or line.startswith("risk ") or line == "risk":
                        self._handle_risk(line)
                        continue
                    if line.startswith("/budget") or line.startswith("budget ") or line == "budget":
                        self._handle_budget(line)
                        continue
                    if line.startswith("/goal") or line.startswith("goal "):
                        await self._handle_goal(line)
                        continue
                    if (
                        line.startswith("/memory")
                        or line.startswith("memory ")
                        or line == "memory"
                    ):
                        await self._handle_memory(line)
                        continue
                    if line.startswith("/remember") or line.startswith("remember "):
                        await self._handle_remember(line)
                        continue
                    if line.startswith("/forget") or line.startswith("forget "):
                        await self._handle_forget(line)
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
            f"  [{_C['dim']}]status[/{_C['dim']}]            organism status panel",
            f"  [{_C['dim']}]cost[/{_C['dim']}]              cost breakdown",
            f"  [{_C['dim']}]cancel <id>[/{_C['dim']}]       cancel an in-flight operation",
            f"  [{_C['dim']}]/risk [tier][/{_C['dim']}]      set risk ceiling",
            f"  [{_C['dim']}]/budget <usd>[/{_C['dim']}]     adjust session budget",
            f"  [{_C['dim']}]/goal [add|rm][/{_C['dim']}]    manage active goals",
            f"  [{_C['dim']}]/memory [...][/{_C['dim']}]     list/add/rm/forbid user-pref memories",
            f"  [{_C['dim']}]/remember <text>[/{_C['dim']}]  shortcut: add a USER memory",
            f"  [{_C['dim']}]/forget <id>[/{_C['dim']}]      shortcut: remove a memory by id",
            f"  [{_C['dim']}]help[/{_C['dim']}]              this message",
            f"  [{_C['dim']}]quit[/{_C['dim']}]              graceful shutdown",
        ]
        panel = Panel(
            "\n".join(lines),
            title="[cyan]🐍 Commands[/cyan]",
            border_style="dim",
            width=min(self._flow.console.width, 52),
            padding=(0, 1),
        )
        self._flow.console.print()
        self._flow.console.print(panel)
        self._flow.console.print()

    async def _handle_cancel(self, op_id: str) -> None:
        """Request cancellation of an in-flight operation."""
        if self._gls is None:
            self._flow.console.print(
                f"  [{_C['death']}]Cancel not available (no GLS reference)[/{_C['death']}]",
                highlight=False,
            )
            return
        if hasattr(self._gls, "request_cancel"):
            found = self._gls.request_cancel(op_id)
            if found:
                self._flow.console.print(
                    f"  [{_C['evolved']}]Cancel requested for {op_id} — will take effect at next phase boundary[/{_C['evolved']}]",
                    highlight=False,
                )
            else:
                self._flow.console.print(
                    f"  [{_C['death']}]No active operation matching '{op_id}'[/{_C['death']}]",
                    highlight=False,
                )
        else:
            self._flow.console.print(
                f"  [{_C['death']}]GLS does not support cancel (upgrade needed)[/{_C['death']}]",
                highlight=False,
            )

    # ── Runtime configuration commands ──────────────────────────

    _VALID_RISK_TIERS = ("safe_auto", "notify_apply", "approval_required", "blocked")

    def _handle_risk(self, line: str) -> None:
        """Set or show the runtime risk tier ceiling.

        Usage: /risk [safe_auto|notify_apply|approval_required]
        Sets JARVIS_RISK_CEILING env var — the orchestrator's GATE phase
        will clamp risk_tier to at most this level.
        """
        parts = line.replace("/risk", "risk", 1).split(None, 1)
        if len(parts) < 2:
            current = os.environ.get("JARVIS_RISK_CEILING", "(not set — using per-op classification)")
            self._flow.console.print(
                f"  [{_C['neural']}]Risk ceiling:[/{_C['neural']}] {current}\n"
                f"  [{_C['dim']}]Usage: /risk safe_auto | notify_apply | approval_required[/{_C['dim']}]",
                highlight=False,
            )
            return
        tier = parts[1].strip().lower()
        if tier not in self._VALID_RISK_TIERS:
            self._flow.console.print(
                f"  [{_C['death']}]Invalid tier '{tier}'. "
                f"Choose: {', '.join(self._VALID_RISK_TIERS[:3])}[/{_C['death']}]",
                highlight=False,
            )
            return
        os.environ["JARVIS_RISK_CEILING"] = tier.upper()
        self._flow.console.print(
            f"  [{_C['evolved']}]Risk ceiling set to {tier.upper()} — "
            f"takes effect on next operation[/{_C['evolved']}]",
            highlight=False,
        )

    def _handle_budget(self, line: str) -> None:
        """Adjust the session budget mid-run.

        Usage: /budget <amount>
        Updates the cost tracker's budget and the harness config.
        """
        parts = line.replace("/budget", "budget", 1).split(None, 1)
        if len(parts) < 2:
            _ct = getattr(self._flow, "_cost_total", 0.0)
            _cap = getattr(self._flow, "_cost_cap", 0.0)
            self._flow.console.print(
                f"  [{_C['neural']}]Budget:[/{_C['neural']}] ${_ct:.4f} / ${_cap:.2f}\n"
                f"  [{_C['dim']}]Usage: /budget <amount_usd>[/{_C['dim']}]",
                highlight=False,
            )
            return
        try:
            amount = float(parts[1].strip().lstrip("$"))
        except ValueError:
            self._flow.console.print(
                f"  [{_C['death']}]Invalid amount. Usage: /budget 1.00[/{_C['death']}]",
                highlight=False,
            )
            return
        if amount <= 0:
            self._flow.console.print(
                f"  [{_C['death']}]Budget must be positive[/{_C['death']}]",
                highlight=False,
            )
            return
        # Update SerpentFlow's cost cap display
        self._flow._cost_cap = amount
        # Update env var for subsystems that read it
        os.environ["OUROBOROS_BATTLE_COST_CAP"] = str(amount)
        self._flow.console.print(
            f"  [{_C['evolved']}]Budget updated to ${amount:.2f}[/{_C['evolved']}]",
            highlight=False,
        )

    async def _handle_goal(self, line: str) -> None:
        """Manage active goals at runtime.

        Usage:
          /goal                     — list active goals
          /goal add <description>   — add a goal (keywords auto-extracted)
          /goal remove <id>         — remove a goal by ID
        """
        parts = line.replace("/goal", "goal", 1).split(None, 2)
        subcmd = parts[1].strip().lower() if len(parts) > 1 else "list"

        # Delegate to harness handler via on_command callback
        # The harness has GoalTracker access; we just format the REPL command
        if self._on_command is not None:
            try:
                result = self._on_command(line)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                self._flow.console.print(
                    f"  [{_C['death']}]Goal error: {exc}[/{_C['death']}]",
                    highlight=False,
                )
        else:
            self._flow.console.print(
                f"  [{_C['dim']}]Goal management requires harness connection[/{_C['dim']}]",
                highlight=False,
            )

    async def _handle_memory(self, line: str) -> None:
        """Manage UserPreferenceStore memories at runtime.

        Usage:
          /memory                         — list all memories
          /memory list [type]             — list (optionally filter by type)
          /memory add <type> <name> | <description>
                                          — add a memory of the given type
          /memory rm <id>                 — remove a memory by id
          /memory forbid <path>           — shortcut: add a FORBIDDEN_PATH memory
          /memory show <id>               — print a single memory's full content
        """
        await self._delegate_to_harness(line, error_label="Memory error")

    async def _handle_remember(self, line: str) -> None:
        """Shortcut: add a free-form USER memory.

        Usage:
          /remember <text>
        """
        await self._delegate_to_harness(line, error_label="Remember error")

    async def _handle_forget(self, line: str) -> None:
        """Shortcut: remove a memory by id.

        Usage:
          /forget <id>
        """
        await self._delegate_to_harness(line, error_label="Forget error")

    async def _delegate_to_harness(self, line: str, *, error_label: str) -> None:
        """Forward the raw line to the harness ``on_command`` callback.

        Shared helper for memory-related commands since they all need the
        harness's ``UserPreferenceStore`` reference — the REPL can't create
        a new store (would lose the in-process singleton the orchestrator
        uses) and can't reach across process boundaries. Errors are
        rendered into the SerpentFlow console with a consistent label.
        """
        if self._on_command is None:
            self._flow.console.print(
                f"  [{_C['dim']}]{error_label}: requires harness connection[/{_C['dim']}]",
                highlight=False,
            )
            return
        try:
            result = self._on_command(line)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            self._flow.console.print(
                f"  [{_C['death']}]{error_label}: {exc}[/{_C['death']}]",
                highlight=False,
            )
