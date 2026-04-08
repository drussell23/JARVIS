"""Serpent Flow — Ouroboros Flowing CLI with Organism Personality.

The serpent doesn't pin a dashboard to your terminal. It flows.
Output streams naturally downward — sensing, synthesizing, evolving —
like watching a living organism think in real time.

Manifesto §7: Absolute Observability — the inner workings of the
symbiote must be entirely visible.

Design:
  - No Rich Live, no Layout, no pinned panels
  - Just Console.print() flowing down the terminal
  - Organism vocabulary: sensed, synthesizing, immune check, evolved, shed
  - Inline syntax-highlighted diffs (Claude Code style + serpent personality)
  - Emoji + color coding for scannable readability
  - Streaming code generation (character-by-character)
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text

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
    """Extract a short display ID from an op_id."""
    if "-" in op_id:
        parts = op_id.split("-")
        return parts[1][:6] if len(parts) > 1 else op_id[:6]
    return op_id[:6]


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

        # Rich console — no Live widget, just print()
        self.console = Console(emoji=True, highlight=False)

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
        if phase_upper in ("CLASSIFY", "ROUTE", "CONTEXT_EXPANSION"):
            return  # These happen fast; don't clutter the flow
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
    ) -> None:
        """Generation completed — show summary."""
        short = _short_id(op_id)
        prov = _prov(provider)
        self._op_providers[op_id] = provider

        tools_str = f" + 🔧 {tool_count} tools" if tool_count > 0 else ""
        self.console.print(
            f"[{_C['neural']}]🧬 synthesized[/{_C['neural']}] │ "
            f"{candidates} candidate{'s' if candidates != 1 else ''} via "
            f"[{_C['provider']}]{prov}[/{_C['provider']}]"
            f"{tools_str}"
            f"  [{_C['dim']}]({duration_s:.1f}s)  op:{short}[/{_C['dim']}]",
            highlight=False,
        )

    # ── Tool calls (Venom) ────────────────────────────────────

    def op_tool_call(
        self, op_id: str, tool_name: str, args_summary: str = "",
        round_index: int = 0, result_preview: str = "",
        duration_ms: float = 0.0, status: str = "success",
    ) -> None:
        """Venom tool call — inline, compact."""
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

    def op_validation(
        self, op_id: str, passed: bool, test_count: int = 0, failures: int = 0,
    ) -> None:
        """Immune check result."""
        short = _short_id(op_id)
        if passed:
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
        tool_count: int = 0,
    ) -> None:
        """Show generated code inline with syntax highlighting."""
        if not candidate_preview:
            return

        c = self.console

        # Detect language
        lang = "python"
        if candidate_files:
            lang = _detect_lang(candidate_files[0])

        # Truncate if huge
        preview = candidate_preview
        truncated = False
        if len(preview) > 2000:
            preview = preview[:2000]
            truncated = True

        try:
            syntax = Syntax(
                preview, lang, theme="monokai",
                line_numbers=False, word_wrap=True,
                padding=(0, 2),
            )
            c.print(syntax)
            if truncated:
                c.print(f"          [{_C['dim']}]... +{len(candidate_preview) - 2000} chars truncated[/{_C['dim']}]")
        except Exception:
            c.print(f"          [{_C['dim']}]{preview[:500]}[/{_C['dim']}]")

        c.print()

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

    # ── Streaming output ──────────────────────────────────────

    def show_streaming_start(self, provider: str, op_id: str = "") -> None:
        """Begin streaming code generation — tokens will appear char-by-char."""
        short = _short_id(op_id) if op_id else ""
        id_str = f"  [{_C['dim']}]op:{short}[/{_C['dim']}]" if short else ""

        if provider:
            prov = _prov(provider)
            via_str = f" via [{_C['provider']}]{prov}[/{_C['provider']}]"
        else:
            via_str = ""

        self.console.print(
            f"[{_C['neural']}]🧬 synthesizing[/{_C['neural']}] │{via_str}{id_str}",
            highlight=False,
        )
        self._streaming_active = True
        # Start dim text for streaming tokens
        sys.stdout.write("          \033[2m")
        sys.stdout.flush()

    def show_streaming_token(self, token: str) -> None:
        """Print a streaming token — character-by-character code writing."""
        if self._streaming_active:
            sys.stdout.write(token)
            sys.stdout.flush()

    def show_streaming_end(self) -> None:
        """End the streaming block."""
        if self._streaming_active:
            sys.stdout.write("\033[0m\n")
            sys.stdout.flush()
            self._streaming_active = False
            self.console.print()

    # ── Operation completion ──────────────────────────────────

    def op_completed(
        self, op_id: str, files_changed: List[str],
        provider: str = "", cost_usd: float = 0.0,
    ) -> None:
        """The organism evolved — operation succeeded."""
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

    async def send(self, msg: Any) -> None:
        """Handle a CommMessage and render via SerpentFlow."""
        try:
            payload = msg.payload if hasattr(msg, "payload") else {}
            op_id = msg.op_id if hasattr(msg, "op_id") else ""
            msg_type = msg.msg_type.value if hasattr(msg, "msg_type") else ""

            if msg_type == "INTENT":
                if payload.get("risk_tier") not in ("routing",):
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

                # Tool call
                elif payload.get("tool_name"):
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
                    )
                    # Show code preview when candidate files present
                    candidate_files = payload.get("candidate_files", [])
                    if candidate_files or payload.get("candidate_preview"):
                        self._flow.show_code_preview(
                            op_id=op_id,
                            provider=provider,
                            candidate_files=candidate_files,
                            candidate_preview=payload.get("candidate_preview", ""),
                            duration_s=payload.get("generation_duration_s", 0.0),
                            tool_count=payload.get("tool_records", 0),
                        )

                # Validation
                elif phase.upper() in ("VALIDATE", "VALIDATE_RETRY") and "test_passed" in payload:
                    self._flow.op_validation(
                        op_id=op_id,
                        passed=payload.get("test_passed", False),
                        test_count=payload.get("test_count", 0),
                        failures=payload.get("test_failures", 0),
                    )

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

                # Streaming code generation tokens
                elif payload.get("streaming") == "start":
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

                # Standard phase transition
                elif phase and ":" not in phase:
                    self._flow.op_phase(
                        op_id=op_id,
                        phase=phase,
                        progress_pct=payload.get("progress_pct", 0.0),
                    )

            elif msg_type == "DECISION":
                outcome = payload.get("outcome", "")
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
                        reason=payload.get("reason_code", outcome),
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
