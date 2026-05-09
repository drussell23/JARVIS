"""``/story`` REPL — §39 Tier-4 operator surface
(PRD v2.73 to v2.74, 2026-05-09).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry``
via §33.3 naming-cage convention.

Combined operator surface for both Tier-4 introspective
modules — ``session`` and ``crystals`` are sister surfaces
under the unified "looking back" frame.

Subcommands:
  /story                          alias for ``help``
  /story session [N]              journal-style narrative for last N sessions
  /story crystals [N]             memory crystallization timeline (N per layer)
  /story status                   master flags + counts
  /story help                     this text
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, List

logger = logging.getLogger(__name__)


STORY_REPL_SCHEMA_VERSION: str = "story_repl.1"


_HELP = (
    "/story — §39 Tier-4 introspective surfaces (PRD)\n"
    "\n"
    "Two sister read-only surfaces:\n"
    "  - 📖 session  : journal-style session narrative (#10)\n"
    "  - 🪨 crystals : memory crystallization timeline (#18)\n"
    "\n"
    "Subcommands:\n"
    "  /story session [N]\n"
    "       Journal-style narrative for last N sessions.\n"
    "       N defaults to JARVIS_SESSION_STORY_MAX_SESSIONS\n"
    "       (default 1; clamped 1..10).\n"
    "\n"
    "  /story crystals [N]\n"
    "       Geological-strata view of memory insights.\n"
    "       N caps crystals shown per layer (default 5).\n"
    "\n"
    "  /story status     master flags + counts\n"
    "  /story help       this text\n"
    "\n"
    "Master flags:\n"
    "  JARVIS_SESSION_STORY_ENABLED              (default false)\n"
    "  JARVIS_MEMORY_CRYSTALLIZATION_ENABLED     (default false)\n"
)


@dataclass(frozen=True)
class StoryReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = STORY_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/story"
        or s == "story"
        or s.startswith("/story ")
        or s.startswith("story ")
    )


def dispatch_story_command(
    line: str,
) -> StoryReplDispatchResult:
    if not _matches(line):
        return StoryReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return StoryReplDispatchResult(
            ok=False,
            text=f"  /story parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "help")

    if head in ("help", "?", ""):
        return StoryReplDispatchResult(
            ok=True, text=_HELP,
        )

    if head == "status":
        return _render_status()

    try:
        if head == "session":
            return _render_session(args[1:])
        if head == "crystals":
            return _render_crystals(args[1:])
        return StoryReplDispatchResult(
            ok=False,
            text=(
                f"  /story: unknown subcommand "
                f"{head!r}. Try /story help."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return StoryReplDispatchResult(
            ok=False,
            text=(
                f"  /story: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _parse_int(args: List[str], *, default: int) -> int:
    if not args:
        return default
    try:
        n = int(args[0])
        return max(1, min(n, 50))
    except (TypeError, ValueError):
        return default


def _render_session(
    args: List[str],
) -> StoryReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.session_story import (  # noqa: E501
            aggregate_session_story,
            format_session_stories, master_enabled,
        )
    except Exception as exc:  # noqa: BLE001
        return StoryReplDispatchResult(
            ok=False,
            text=(
                "  /story session: substrate unavailable "
                f"({type(exc).__name__})"
            ),
        )
    if not master_enabled():
        return StoryReplDispatchResult(
            ok=False,
            text=(
                "  /story session: session story disabled "
                "(default per §33.1). Set "
                "JARVIS_SESSION_STORY_ENABLED=true."
            ),
        )
    n = _parse_int(args, default=1)
    stories = aggregate_session_story(n_sessions=n)
    if not stories:
        return StoryReplDispatchResult(
            ok=True,
            text=(
                "# /story session — no parseable session "
                "records found"
            ),
        )
    out = format_session_stories(stories)
    if not out:
        return StoryReplDispatchResult(
            ok=True,
            text=(
                "# /story session — (empty render)"
            ),
        )
    return StoryReplDispatchResult(ok=True, text=out)


def _render_crystals(
    args: List[str],
) -> StoryReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.memory_crystallization import (  # noqa: E501
            format_crystal_timeline, master_enabled,
        )
    except Exception as exc:  # noqa: BLE001
        return StoryReplDispatchResult(
            ok=False,
            text=(
                "  /story crystals: substrate unavailable "
                f"({type(exc).__name__})"
            ),
        )
    if not master_enabled():
        return StoryReplDispatchResult(
            ok=False,
            text=(
                "  /story crystals: crystallization "
                "disabled (default per §33.1). Set "
                "JARVIS_MEMORY_CRYSTALLIZATION_ENABLED="
                "true."
            ),
        )
    cap = _parse_int(args, default=5)
    out = format_crystal_timeline(crystals_per_layer=cap)
    if not out:
        return StoryReplDispatchResult(
            ok=True,
            text=(
                "# /story crystals — no insights "
                "(insights.jsonl missing or empty at "
                ".jarvis/ouroboros/consciousness/)"
            ),
        )
    return StoryReplDispatchResult(ok=True, text=out)


def _render_status() -> StoryReplDispatchResult:
    parts = ["# /story status"]
    try:
        from backend.core.ouroboros.governance.session_story import (  # noqa: E501
            master_enabled as story_master,
        )
        parts.append(
            f"  session_story_master       : "
            f"{story_master()}"
        )
    except Exception:  # noqa: BLE001
        parts.append(
            "  session_story_master       : (unavailable)"
        )
    try:
        from backend.core.ouroboros.governance.memory_crystallization import (  # noqa: E501
            aggregate_crystal_timeline,
            master_enabled as crystal_master,
        )
        parts.append(
            f"  crystallization_master     : "
            f"{crystal_master()}"
        )
        if crystal_master():
            timeline = aggregate_crystal_timeline()
            parts.append(
                f"  total_insights             : "
                f"{timeline.total_insights}"
            )
            for age_value, count in timeline.by_age.items():
                if count > 0:
                    parts.append(
                        f"    {age_value:<13} : {count}"
                    )
    except Exception:  # noqa: BLE001
        parts.append(
            "  crystallization_master     : (unavailable)"
        )
    return StoryReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            verb="story",
            description=(
                "Introspective surfaces — session story + "
                "memory crystallization timeline"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "story_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "STORY_REPL_SCHEMA_VERSION",
    "StoryReplDispatchResult",
    "dispatch_story_command",
    "register_verbs",
]
