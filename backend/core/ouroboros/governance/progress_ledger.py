"""Slice 202 — Git-tracked Progress Ledger (the Ralph legibility pattern).

snarktank/ralph keeps a dead-simple, git-tracked, human-readable progress file
so both the operator and the loop can see "what's done / what's next" at a
glance. O+V's progress lives in ``.jarvis`` (gitignored, signed YAML, JSON
summaries) — durable but not legible in the repo. This module adds a
``progress.txt`` at the repo root: a plain, committable ledger the organism
updates as roadmap sub-steps complete.

Gated ``JARVIS_PROGRESS_LEDGER_ENABLED`` default-FALSE. Fail-soft — a write
failure never blocks the loop. The file is plain text (git-diff-friendly);
the AutoCommitter (or an operator) commits it like any other tracked file.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_PROGRESS_LEDGER_ENABLED"
_DEFAULT_PATH = "progress.txt"


def ledger_enabled() -> bool:
    """Gate, default FALSE. NEVER raises."""
    return os.environ.get(_ENV_ENABLED, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def render_progress(
    completed: Sequence[Tuple[str, str]],
    next_targets: Sequence[Tuple[str, str]],
) -> str:
    """Render a human-readable progress ledger. NEVER raises."""
    try:
        lines: List[str] = [
            "# O+V Strategic Progress Ledger",
            "# Auto-maintained by the organism (Slice 202). Git-tracked for",
            "# operator legibility. Advisory — authority stays with the gates.",
            "",
            "## COMPLETED",
        ]
        if completed:
            for gid, summary in completed:
                lines.append(f"  [x] {gid}: {summary}")
        else:
            lines.append("  (none yet)")
        lines += ["", "## NEXT TARGETS"]
        if next_targets:
            for gid, summary in next_targets:
                lines.append(f"  [ ] {gid}: {summary}")
        else:
            lines.append("  (none queued)")
        lines.append("")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return "# O+V Strategic Progress Ledger\n(render error)\n"


def update_progress(
    completed: Sequence[Tuple[str, str]],
    next_targets: Sequence[Tuple[str, str]],
    path: Optional[Path] = None,
) -> Optional[Path]:
    """Write the ledger to ``path`` (repo-root ``progress.txt`` by default).
    Returns the path on success, else None. NEVER raises."""
    try:
        target = Path(path) if path is not None else Path(_DEFAULT_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            render_progress(completed, next_targets), encoding="utf-8",
        )
        return target
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ProgressLedger] update failed soft: %s", exc)
        return None
