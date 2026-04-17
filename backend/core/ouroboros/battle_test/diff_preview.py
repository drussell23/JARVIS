"""Rich NOTIFY_APPLY (Yellow-tier) diff preview renderer — V1.

Closes the "is the diff shown during the 5s auto-apply window good enough
to trust?" UX gap. The legacy preview truncates a raw unified-diff string
to 4000 chars and dumps it as plain text — effectively a wall of text
that operators skim rather than read. Yellow tier is exactly the tier
where operator trust is load-bearing: the system is going to apply, and
the 5s notice is meant to be a last-chance review.

V1 deliverable (this module):
  • File-tree breakdown (Rich.Tree) for multi-file candidates
  • Per-file Panels with status badges ([+ new] / [~ modified] / [− deleted])
  • Diff body with Pygments ``diff`` lexer (+/- markers coloured)
  • Per-file head+tail truncation at ``JARVIS_DIFF_PREVIEW_MAX_LINES_PER_FILE``
  • Binary-file safeguard (detect non-text, show ``[binary, N bytes]``)
  • Stats roll-up (``+X/-Y across N files``) in header
  • Live countdown with cancel-poll (driven by SerpentFlow wrapper)
  • Env kill-switch ``JARVIS_UI_DIFF_PREVIEW_ENABLED`` (default on)
  • TTY gate — disable in non-TTY contexts even when env is on
  • Safe fallback — if any render step raises, SerpentFlow catches and
    reverts to the legacy plain-text preview so NOTIFY_APPLY never
    deadlocks on a preview bug
  • Optional on-disk dump — ``JARVIS_DIFF_PREVIEW_DUMP_PATH`` — writes
    the full unified diff to ``<path>/<op_id>.diff`` for review outside
    the 5s window (silent on unset; never fails loudly)

Deferred to V1.1 (scope-locked):
  • Side-by-side layout (requires ≥160-col responsive detection)
  • Deep omission compression (collapse unchanged runs with glyphs)
  • Line-number gutters on both sides
  • Per-line Pygments lexing by source language (not just ``diff`` lexer)

Authority invariant: the renderer writes ONLY to the operator terminal
(and optionally a dump file). It does not mutate the candidate, the
operation context, the risk tier, or the cancel flag. /reject remains
the sole cancellation channel.
"""
from __future__ import annotations

import difflib
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence

logger = logging.getLogger("Ouroboros.DiffPreview")

_ENV_ENABLED = "JARVIS_UI_DIFF_PREVIEW_ENABLED"
_ENV_MAX_LINES = "JARVIS_DIFF_PREVIEW_MAX_LINES_PER_FILE"
_ENV_CONTEXT_LINES = "JARVIS_DIFF_PREVIEW_CONTEXT_LINES"
_ENV_DUMP_PATH = "JARVIS_DIFF_PREVIEW_DUMP_PATH"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def preview_enabled() -> bool:
    """Env-gate read. Default: ON. Set to 0 to force-disable."""
    return os.environ.get(_ENV_ENABLED, "1").strip().lower() in _TRUTHY


def _max_lines_per_file() -> int:
    try:
        return max(20, int(os.environ.get(_ENV_MAX_LINES, "200")))
    except (TypeError, ValueError):
        return 200


def _context_lines() -> int:
    try:
        return max(0, int(os.environ.get(_ENV_CONTEXT_LINES, "3")))
    except (TypeError, ValueError):
        return 3


def _dump_path_setting() -> str:
    return os.environ.get(_ENV_DUMP_PATH, "").strip()


# ---------------------------------------------------------------------------
# FileChange — the data shape the renderer consumes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileChange:
    """One file's proposed change, with enough context to render a diff.

    ``old_content`` is the pre-apply content read from disk (may be empty
    string for new files — the file did not exist yet). ``new_content``
    is the model's proposed post-apply content. ``rationale`` is the
    per-file reasoning string the model emitted (empty for legacy
    single-file candidates).

    ``status`` is derived from the old/new pair if not stamped explicitly:
      • ``"new"``      — old_content == "" and file did not exist
      • ``"deleted"``  — new_content == "" and old_content != ""
      • ``"modified"`` — both non-empty and differ
      • ``"unchanged"`` — both identical (rendered with a muted header;
                         primarily a sentinel for diagnostics)
    """

    path: str
    old_content: str = ""
    new_content: str = ""
    rationale: str = ""
    status: str = ""  # Computed in __post_init__ if empty
    is_binary: bool = False

    def __post_init__(self) -> None:
        # dataclass is frozen — mutate via object.__setattr__
        if not self.status:
            derived = _derive_status(
                self.old_content, self.new_content,
            )
            object.__setattr__(self, "status", derived)

    @property
    def added_lines(self) -> int:
        if self.is_binary:
            return 0
        if self.status == "deleted":
            return 0
        if self.status == "new":
            return len(self.new_content.splitlines())
        old = self.old_content.splitlines()
        new = self.new_content.splitlines()
        return sum(1 for line in difflib.ndiff(old, new) if line.startswith("+ "))

    @property
    def removed_lines(self) -> int:
        if self.is_binary:
            return 0
        if self.status == "new":
            return 0
        if self.status == "deleted":
            return len(self.old_content.splitlines())
        old = self.old_content.splitlines()
        new = self.new_content.splitlines()
        return sum(1 for line in difflib.ndiff(old, new) if line.startswith("- "))


def _derive_status(old: str, new: str) -> str:
    if old == "" and new != "":
        return "new"
    if old != "" and new == "":
        return "deleted"
    if old == new:
        return "unchanged"
    return "modified"


def _looks_binary(content: str) -> bool:
    """Cheap heuristic: a NUL byte or >30% non-printable chars flags binary."""
    if not content:
        return False
    if "\x00" in content:
        return True
    sample = content[:4096]
    if not sample:
        return False
    nonprintable = sum(
        1 for c in sample
        if ord(c) < 9 or (13 < ord(c) < 32)
    )
    return (nonprintable / max(1, len(sample))) > 0.30


# ---------------------------------------------------------------------------
# Preview renderer
# ---------------------------------------------------------------------------


@dataclass
class DiffPreviewRenderer:
    """Stateless builder — call :meth:`build` to produce a Rich renderable.

    Uses ``Rich.Tree`` for multi-file breakdown, ``Rich.Panel`` per file,
    and ``Rich.Syntax`` with the ``diff`` lexer for the diff body. All
    colors and glyphs are sourced from SerpentFlow's existing palette
    where possible; the renderer degrades to Rich defaults if the
    palette isn't importable.
    """

    max_lines_per_file: int = field(default_factory=_max_lines_per_file)
    context_lines: int = field(default_factory=_context_lines)

    # ------------------------------------------------------------------
    # Public render API
    # ------------------------------------------------------------------

    def build(
        self,
        *,
        op_id: str,
        reason: str,
        changes: Sequence[FileChange],
        delay_remaining_s: float = 0.0,
    ) -> Any:
        """Return a Rich renderable composing header + tree + per-file panels + footer.

        Never raises: any per-file render failure degrades to a plain-text
        fallback line for that file so the overall preview still renders.
        """
        from rich.console import Group
        from rich.panel import Panel
        from rich.text import Text

        # ---- Header ---------------------------------------------------
        total_added = sum(c.added_lines for c in changes)
        total_removed = sum(c.removed_lines for c in changes)
        n_files = len(changes)
        header_line = Text()
        header_line.append("⚠ NOTIFY_APPLY", style="bold yellow")
        header_line.append("  ")
        header_line.append(f"op={op_id}", style="dim")
        header_line.append("  •  ")
        header_line.append(f"reason: {reason or 'n/a'}", style="cyan")
        header_line.append("  •  ")
        header_line.append(
            f"+{total_added}/-{total_removed} lines across {n_files} "
            f"file{'s' if n_files != 1 else ''}",
            style="bold",
        )

        parts: List[Any] = [header_line]

        # ---- File tree (multi-file only) ------------------------------
        if n_files > 1:
            parts.append(self._build_file_tree(changes))

        # ---- Per-file panels ------------------------------------------
        for change in changes:
            parts.append(self._build_file_panel(change))

        # ---- Countdown footer ----------------------------------------
        footer = Text()
        if delay_remaining_s > 0:
            footer.append("Applying in ", style="dim")
            footer.append(f"{delay_remaining_s:.1f}s", style="bold green")
            footer.append("  —  ", style="dim")
            footer.append("/reject", style="bold red")
            footer.append(" to cancel", style="dim")
        else:
            footer.append("Applying…", style="bold green")
        parts.append(footer)

        return Panel(
            Group(*parts),
            title="[bold yellow]Yellow Tier — Auto-apply Preview[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_file_tree(self, changes: Sequence[FileChange]) -> Any:
        from rich.tree import Tree
        from rich.text import Text
        tree = Tree(
            Text.assemble(
                ("📁 Changed files ", "bold"),
                (f"({len(changes)})", "dim"),
            ),
            guide_style="dim",
        )
        for c in changes:
            label = Text()
            label.append(c.path)
            label.append("  ")
            label.append(_badge(c.status))
            if not c.is_binary and c.status != "unchanged":
                label.append("  ")
                label.append(f"+{c.added_lines}", style="green")
                label.append("/", style="dim")
                label.append(f"-{c.removed_lines}", style="red")
            elif c.is_binary:
                label.append("  ")
                label.append("[binary]", style="dim")
            tree.add(label)
        return tree

    def _build_file_panel(self, change: FileChange) -> Any:
        from rich.panel import Panel
        from rich.console import Group
        from rich.text import Text

        header = Text()
        header.append(change.path, style="bold")
        header.append("  ")
        header.append(_badge(change.status))
        if not change.is_binary and change.status != "unchanged":
            header.append("  ")
            header.append(f"+{change.added_lines}", style="green")
            header.append("/", style="dim")
            header.append(f"-{change.removed_lines}", style="red")

        parts: List[Any] = [header]

        if change.rationale:
            rationale = Text()
            rationale.append("rationale: ", style="dim italic")
            rationale.append(change.rationale, style="italic")
            parts.append(rationale)

        body = self._build_diff_body(change)
        parts.append(body)

        return Panel(
            Group(*parts),
            border_style=_status_color(change.status),
            padding=(0, 1),
        )

    def _build_diff_body(self, change: FileChange) -> Any:
        """Compute unified diff, truncate, render via Pygments diff lexer."""
        from rich.syntax import Syntax
        from rich.text import Text

        if change.is_binary:
            return Text(
                f"[binary file — not diffable, {len(change.new_content)} bytes]",
                style="dim italic",
            )

        if change.status == "unchanged":
            return Text("(no textual changes)", style="dim italic")

        try:
            diff_lines = list(
                difflib.unified_diff(
                    change.old_content.splitlines(keepends=False),
                    change.new_content.splitlines(keepends=False),
                    fromfile=f"a/{change.path}",
                    tofile=f"b/{change.path}",
                    n=self.context_lines,
                    lineterm="",
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "[DiffPreview] unified_diff failed for %s", change.path,
                exc_info=True,
            )
            return Text(
                "(diff generation failed — see dump file if enabled)",
                style="dim italic",
            )

        if not diff_lines:
            return Text("(identical content)", style="dim italic")

        truncated, omitted = _truncate_head_tail(
            diff_lines, self.max_lines_per_file,
        )
        diff_text = "\n".join(truncated)

        try:
            body = Syntax(
                diff_text, "diff", theme="monokai",
                word_wrap=False, line_numbers=False,
            )
        except Exception:  # noqa: BLE001
            # Safe fallback: plain Text. Still readable, still coloured by
            # prefix via regex not available here — accept flat text.
            logger.debug(
                "[DiffPreview] Syntax lexer failed for %s", change.path,
                exc_info=True,
            )
            body = Text(diff_text)

        if omitted > 0:
            from rich.console import Group
            note = Text(
                f"… {omitted} lines omitted (head+tail shown; cap "
                f"JARVIS_DIFF_PREVIEW_MAX_LINES_PER_FILE={self.max_lines_per_file}) …",
                style="dim italic",
            )
            return Group(body, note)
        return body


# ---------------------------------------------------------------------------
# Helpers — badges, truncation, dump-to-disk
# ---------------------------------------------------------------------------


def _badge(status: str) -> "Text":  # type: ignore[name-defined]
    from rich.text import Text
    t = Text()
    if status == "new":
        t.append("[+ new]", style="bold green")
    elif status == "deleted":
        t.append("[− deleted]", style="bold red")
    elif status == "modified":
        t.append("[~ modified]", style="bold yellow")
    elif status == "unchanged":
        t.append("[= unchanged]", style="dim")
    else:
        t.append(f"[? {status}]", style="dim")
    return t


def _status_color(status: str) -> str:
    return {
        "new": "green",
        "modified": "yellow",
        "deleted": "red",
        "unchanged": "dim",
    }.get(status, "white")


def _truncate_head_tail(lines: List[str], max_lines: int) -> tuple:
    """Head+tail truncation. If input fits, returns (input, 0).

    Otherwise keeps the first ``head`` lines and last ``tail`` lines,
    where ``head + tail + 1 (omission marker)`` ≤ ``max_lines``. Returns
    ``(truncated_lines, omitted_count)``.
    """
    if len(lines) <= max_lines:
        return (lines, 0)
    # Reserve one line for the "... N omitted ..." marker injected by
    # the caller (as a separate renderable, not within this list).
    budget = max_lines - 1
    head_n = budget // 2
    tail_n = budget - head_n
    omitted = len(lines) - head_n - tail_n
    truncated = lines[:head_n] + ["…"] + lines[-tail_n:]
    return (truncated, omitted)


def dump_full_diff(
    op_id: str, changes: Sequence[FileChange], dump_dir: Optional[str] = None,
) -> Optional[Path]:
    """Write the full untruncated unified diff for all files to disk.

    Enabled when ``JARVIS_DIFF_PREVIEW_DUMP_PATH`` is set (or ``dump_dir``
    is passed explicitly). Silent no-op when unset. Never raises — any
    OS error returns ``None`` after a DEBUG log.

    Returns the written :class:`~pathlib.Path` on success, else ``None``.
    """
    target_dir = (dump_dir or _dump_path_setting() or "").strip()
    if not target_dir:
        return None
    try:
        out_dir = Path(target_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        # Sanitize op_id for filename — strip anything weird.
        safe_op = "".join(
            c if c.isalnum() or c in ("-", "_") else "_"
            for c in (op_id or "unknown")
        )
        out_path = out_dir / f"{safe_op}.diff"
        buf: List[str] = []
        buf.append(f"# NOTIFY_APPLY diff dump — op_id={op_id}\n")
        buf.append(f"# files: {len(changes)}\n\n")
        for c in changes:
            buf.append(f"=== {c.path}  [{c.status}]  ")
            buf.append(f"(+{c.added_lines}/-{c.removed_lines})")
            if c.rationale:
                buf.append(f"  rationale: {c.rationale}")
            buf.append(" ===\n")
            if c.is_binary:
                buf.append(f"[binary file — {len(c.new_content)} bytes, omitted]\n\n")
                continue
            try:
                diff_lines = difflib.unified_diff(
                    c.old_content.splitlines(keepends=False),
                    c.new_content.splitlines(keepends=False),
                    fromfile=f"a/{c.path}",
                    tofile=f"b/{c.path}",
                    n=3,
                    lineterm="",
                )
                buf.append("\n".join(diff_lines))
                buf.append("\n\n")
            except Exception:  # noqa: BLE001
                buf.append("(diff generation failed)\n\n")
        out_path.write_text("".join(buf), encoding="utf-8")
        return out_path
    except Exception:  # noqa: BLE001
        logger.debug(
            "[DiffPreview] dump_full_diff failed for op=%s", op_id,
            exc_info=True,
        )
        return None


def should_render(console: Any = None) -> bool:
    """Combined gate: env enabled AND console is a real terminal.

    When ``console`` is None, falls back to ``sys.stdout.isatty()``. The
    TTY check prevents the rich preview from emitting in background
    runs / CI / piped contexts — it would just be noise there.
    """
    if not preview_enabled():
        return False
    if console is not None:
        is_term = getattr(console, "is_terminal", None)
        if is_term is not None:
            return bool(is_term)
    # Fallback: stdout heuristic
    try:
        return bool(sys.stdout.isatty())
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Candidate → FileChange extraction
# ---------------------------------------------------------------------------


def build_changes_from_candidate(
    candidate: dict,
    repo_root: Path,
) -> List[FileChange]:
    """Convert a single- or multi-file candidate into a FileChange list.

    Reads pre-apply file contents from disk (files are still untouched
    at NOTIFY_APPLY time — APPLY hasn't run yet). For paths that don't
    exist yet, ``old_content`` becomes ``""`` and status resolves to
    ``"new"``. Binary files are detected and flagged; their diff body
    is replaced with a ``[binary]`` sentinel.

    Orchestrator's ``_iter_candidate_files`` is the canonical unpacker
    for candidate shapes; this function mirrors its logic so the preview
    sees exactly what APPLY will see.
    """
    changes: List[FileChange] = []
    multi_enabled = (
        os.environ.get("JARVIS_MULTI_FILE_GEN_ENABLED", "true").lower()
        not in ("false", "0", "no", "off")
    )
    files_field = candidate.get("files") if multi_enabled else None

    # Multi-file shape takes precedence when present + enabled.
    pairs: List[tuple] = []  # (path, new_content, rationale)
    if isinstance(files_field, list) and files_field:
        seen: set = set()
        for entry in files_field:
            if not isinstance(entry, dict):
                continue
            fp = str(entry.get("file_path", "") or "")
            fc = entry.get("full_content", "") or ""
            rat = str(entry.get("rationale", "") or "")
            if not fp or not isinstance(fc, str):
                continue
            if fp in seen:
                continue
            seen.add(fp)
            pairs.append((fp, fc, rat))

    if not pairs:
        # Legacy single-file candidate.
        fp = str(candidate.get("file_path", "") or "")
        fc = candidate.get("full_content", "") or ""
        if fp and isinstance(fc, str):
            pairs = [(fp, fc, "")]

    for fp, fc, rat in pairs:
        old = ""
        try:
            abs_path = (repo_root / fp) if not Path(fp).is_absolute() else Path(fp)
            if abs_path.is_file():
                try:
                    old = abs_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    # Non-UTF8 or binary on disk
                    try:
                        raw = abs_path.read_bytes()
                        old = raw.decode("utf-8", errors="replace")
                    except Exception:  # noqa: BLE001
                        old = ""
        except Exception:  # noqa: BLE001
            logger.debug(
                "[DiffPreview] failed to read pre-apply content: %s", fp,
                exc_info=True,
            )
            old = ""

        is_bin = _looks_binary(old) or _looks_binary(fc)
        changes.append(
            FileChange(
                path=fp,
                old_content=old,
                new_content=fc,
                rationale=rat,
                is_binary=is_bin,
            )
        )
    return changes
