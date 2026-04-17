"""TODO/FIXME deadline gate — example GatePlugin.

Flags candidates that introduce new TODO/FIXME/XXX comments without
an adjacent YYYY-MM-DD deadline marker. Soft severity — downgrades
SAFE_AUTO to NOTIFY_APPLY so operators see the undated TODO in the
diff preview before it merges.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from backend.core.ouroboros.plugins.plugin_base import GatePlugin


# Match a TODO/FIXME/XXX comment, case-insensitive, word-bounded.
_TODO_RE = re.compile(
    r"#.*\b(TODO|FIXME|XXX)\b",
    re.IGNORECASE,
)
# ISO date marker the gate accepts as "owned + deadline."
_DEADLINE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _count_undated(content: str) -> int:
    n = 0
    for line in (content or "").splitlines():
        if _TODO_RE.search(line) and not _DEADLINE_RE.search(line):
            n += 1
    return n


class TodoDeadlineGate(GatePlugin):
    pattern_name = "todo_without_deadline"
    severity = "soft"

    def inspect(
        self,
        *,
        file_path: str,
        old_content: str,
        new_content: str,
    ) -> Optional[Tuple[str, str]]:
        delta = _count_undated(new_content) - _count_undated(old_content)
        if delta <= 0:
            return None
        return (
            "soft",
            f"{delta} new TODO/FIXME/XXX without YYYY-MM-DD deadline marker",
        )
