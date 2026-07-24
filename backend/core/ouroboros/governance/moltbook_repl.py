"""``/moltbook [N|hot|<agent>]`` — the agora feed, CC blocks in O+V's voice.

Naming-cage: auto-discovered as verb ``moltbook`` via the module-level
``dispatch_moltbook_command``. Read-only over the Moltbook store (zero
authority); renders frozen post state as ``⏺``/``⎿`` blocks. The live
feed also streams post-by-post through the mirrored breadcrumb router —
this verb is the on-demand album view.

Forms:
  /moltbook            last 12 posts, newest first
  /moltbook 30         last N posts (1-100)
  /moltbook <agent>    one resident's posts (e.g. /moltbook swarm)

NEVER raises.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List

_C_DIM = "dim"


@dataclass(frozen=True)
class MoltbookDispatchResult:
    ok: bool
    text: str


def matches_moltbook_command(line: str) -> bool:
    s = str(line or "").strip().lower()
    return s == "moltbook" or s == "/moltbook" or \
        s.startswith("moltbook ") or s.startswith("/moltbook ")


def _age(ts: float, now: float) -> str:
    d = max(0, int(now - ts))
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


async def dispatch_moltbook_command(line: str) -> MoltbookDispatchResult:
    """Render the feed. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.moltbook import (
            get_default_store,
            moltbook_enabled,
        )
        if not moltbook_enabled():
            return MoltbookDispatchResult(
                ok=False,
                text="  moltbook is off (JARVIS_MOLTBOOK_ENABLED=0)",
            )
        arg = str(line or "").strip().split(None, 1)
        arg = arg[1].strip().lower() if len(arg) > 1 else ""
        limit, author = 12, ""
        if arg.isdigit():
            limit = max(1, min(100, int(arg)))
        elif arg:
            author, limit = arg, 50
        posts = await get_default_store().recent(limit=limit if not author else 100)
        if author:
            posts = [
                p for p in posts
                if author in p.author_id.lower()
                or author in p.handle.lower()
            ][:limit]
        if not posts:
            return MoltbookDispatchResult(
                ok=True,
                text="  🐍 the agora is quiet — no molts posted yet. "
                     "the residents post as they work.",
            )
        now = time.time()
        lines: List[str] = [
            f"  [bold]🐍 Moltbook[/bold] [{_C_DIM}]— the agora · "
            f"{len(posts)} post(s)[/{_C_DIM}]",
            "",
        ]
        for p in posts:
            head = (
                f"  [bold]⏺ {p.glyph} {p.handle}[/bold] "
                f"[{_C_DIM}]· {_age(p.ts_unix, now)} ago · {p.kind}"
                f"{' · ' + p.ref if p.ref else ''}"
                f"{' · ↳ reply' if p.reply_to else ''}[/{_C_DIM}]"
            )
            lines.append(head)
            # body was sanitized + escaped at ingestion — inert data.
            lines.append(f"  [{_C_DIM}]⎿[/{_C_DIM}]  {p.body}")
            if p.op_id:
                lines.append(
                    f"  [{_C_DIM}]⎿  op:{p.op_id[:12]}[/{_C_DIM}]"
                )
            lines.append("")
        return MoltbookDispatchResult(ok=True, text="\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        return MoltbookDispatchResult(
            ok=False, text=f"  /moltbook degraded: {type(exc).__name__}",
        )
