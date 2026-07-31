"""What the organism remembers, made visible.

O+V carries fifteen memory modules and, in this repo, 764 written topics. The
cockpit had no verb for any of it. An operator could see what the organism was
DOING at every phase and had no way to ask what it KNEW — which is the half
that explains the doing.

That gap matters more here than it would in a reactive tool. O+V acts
unprompted, so the operator's question is never only "what did it do" but
"what did it believe when it decided to". Memory is the answer to the second,
and an answer nobody can read is not an answer.

Reads, never writes
-------------------
This composes lines from stores that already exist and owns none of them.
`UserPreferenceStore` is the authority on preferences, `docs/memory_topics` on
architecture, `ModuleContextRouter` on what gets injected where. A surface
that could edit memory would be a second write path into state the governance
pipeline depends on — and the one place a display must not become is an
authority.

The same reason `/posture`'s override is the only write-surface it has, and
the same reason the blast-radius gutter reads through `peek` and never scans.

Degrades per-SOURCE, not globally
----------------------------------
Each section is gathered independently and a failure in one is reported in
place rather than emptying the panel. A memory surface that renders nothing
because one store is unavailable teaches the operator that the organism
remembers nothing, which is the opposite of true and worse than a partial
answer that says which part is missing.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.MemorySurface")

MEMORY_SURFACE_SCHEMA_VERSION: str = "memory_surface.1"

__all__ = [
    "MEMORY_SURFACE_SCHEMA_VERSION",
    "compose_memory_lines",
    "memory_surface_enabled",
    "preference_rows",
    "search_topics",
    "topic_counts",
]

#: How many rows a bare `/memory` may spend. The cockpit deck is finite and a
#: verb that floods it is one an operator stops typing — the restraint the
#: rest of this surface already keeps.
_DEFAULT_LIMIT = 12


def memory_surface_enabled() -> bool:
    """``JARVIS_MEMORY_SURFACE_ENABLED`` (default true). NEVER raises."""
    return os.environ.get(
        "JARVIS_MEMORY_SURFACE_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


#: Hard ceiling on what one `/memory` may put on the wire.
#:
#: The verb sends per-domain COUNTS and a capped hit list — never the corpus —
#: so a bare `/memory` is ~700 bytes and a search is bounded by `limit`. This
#: is the guard that keeps it that way: a future section, or a corpus that
#: grows an order of magnitude, cannot quietly turn a summary into a flood
#: that costs the TUI its frame budget.
#:
#: Enforced by TRUNCATION with a visible marker rather than by refusing to
#: answer. An operator who asked what the organism knows should get a short
#: answer that says it was shortened, never an error.
_MAX_PAYLOAD_BYTES = 8192


def _tracked_topic_paths(root: Path) -> Optional[List[Path]]:
    """The topic files the REPO declares, or None if git cannot say.

    This is the root fix, and it is not a filter.

    The corpus is not "every .md under a directory" — it is what the
    repository tracks. Reading the working tree instead made the surface
    count things the repo has explicitly disowned: iCloud conflict copies
    (`battle_test 2/`, created because this repo lives under a synced
    `~/Documents`) are UNTRACKED and already gitignored at `.gitignore:270`.
    Nothing in iCloud is part of O+V's memory; the sync client simply
    littered inside the working tree.

    So the answer is to ask the authority that already knows. That
    generalises past iCloud for free — editor backups, a vendored second
    checkout, stray downloads — because "ignored" is the repo stating that a
    file is not part of itself, and no pattern list has to be maintained to
    keep up.

    Returns None (not an empty list) when git is unavailable, so the caller
    can fall back to walking the tree rather than reporting a corpus of zero.
    An absent VCS must not read as an empty mind.
    """
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "ls-files", "-z", "--", "docs/memory_topics"],
            cwd=str(root), capture_output=True, timeout=10.0, check=False,
        )
        if proc.returncode != 0:
            return None
        names = [n for n in proc.stdout.decode(
            "utf-8", "replace").split("\0") if n.endswith(".md")]
        return [root / n for n in names]
    except Exception:  # noqa: BLE001
        logger.debug("[MemorySurface] git ls-files unavailable", exc_info=True)
        return None


def _repo_root() -> Path:
    """The repo root, derived from THIS file's location. NEVER raises.

    Derived rather than read from an env var: a memory surface that pointed
    at the wrong tree would report another project's knowledge as this one's,
    and be entirely convincing while doing it.
    """
    try:
        return Path(__file__).resolve().parents[4]
    except Exception:  # noqa: BLE001
        return Path.cwd()


# ---------------------------------------------------------------------------
# preferences — what the operator taught it
# ---------------------------------------------------------------------------


def preference_rows(limit: int = _DEFAULT_LIMIT) -> Tuple[List[str], str]:
    """``(rows, error)`` from `UserPreferenceStore`. NEVER raises.

    Returns the ERROR rather than logging it and yielding nothing: "the
    preference store is unavailable" and "you have taught it nothing" render
    identically as an empty list, and they are opposite facts.
    """
    try:
        from backend.core.ouroboros.governance.user_preference_memory import (
            get_default_store,
        )

        memories = list(get_default_store().list_all() or ())
        if not memories:
            return [], ""
        rows: List[str] = []
        for mem in memories[: max(1, int(limit))]:
            kind = getattr(getattr(mem, "type", None), "value", "?")
            name = str(getattr(mem, "name", "") or getattr(mem, "id", "?"))
            desc = str(getattr(mem, "description", "") or "").strip()
            rows.append(f"    [{kind}] {name} — {desc}" if desc
                        else f"    [{kind}] {name}")
        if len(memories) > limit:
            rows.append(f"    … {len(memories) - limit} more")
        return rows, ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("[MemorySurface] preferences degraded", exc_info=True)
        return [], f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# topics — what it was told about its own architecture
# ---------------------------------------------------------------------------


def topic_counts() -> Dict[str, int]:
    """``{domain: count}`` under `docs/memory_topics`. NEVER raises.

    Counted from disk rather than from an index file. `INDEX.md` is written
    by hand and can lag; the directory cannot, and a memory surface that
    reports a stale count is telling the operator something false about how
    much the organism knows.
    """
    out: Dict[str, int] = {}
    try:
        root = _repo_root() / "docs" / "memory_topics"
        if not root.is_dir():
            return out
        tracked = _tracked_topic_paths(_repo_root())
        if tracked is not None:
            for path in tracked:
                domain = path.parent.name
                out[domain] = out.get(domain, 0) + 1
            return out
        # git unavailable — walk the tree, and say nothing false about it.
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            n = sum(1 for _ in child.glob("*.md"))
            if n:
                out[child.name] = n
    except Exception:  # noqa: BLE001
        logger.debug("[MemorySurface] topic scan degraded", exc_info=True)
    return out


def search_topics(term: str, limit: int = _DEFAULT_LIMIT) -> List[str]:
    """Topic paths whose NAME matches ``term``. NEVER raises.

    Filename-only, deliberately. Grepping 764 bodies from a key handler would
    block the event loop on a full-screen Application — the render stalls the
    async-diff work already had to move off-thread to avoid. Names are
    descriptive here by convention (`project_*`, `feedback_*`, `slice*`), so
    the cheap match is also the useful one; `/expand` on a hit reads the body.
    """
    try:
        needle = str(term or "").strip().lower()
        if not needle:
            return []
        root = _repo_root() / "docs" / "memory_topics"
        if not root.is_dir():
            return []
        hits: List[str] = []
        tracked = _tracked_topic_paths(_repo_root())
        candidates = (sorted(tracked) if tracked is not None
                      else sorted(root.rglob("*.md")))
        for path in candidates:
            if needle in path.name.lower():
                hits.append(f"    {path.parent.name}/{path.name}")
                if len(hits) >= max(1, int(limit)):
                    break
        return hits
    except Exception:  # noqa: BLE001
        logger.debug("[MemorySurface] topic search degraded", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# routing — where memory actually lands in a prompt
# ---------------------------------------------------------------------------


def _routing_row() -> str:
    """One line on `ModuleContextRouter`. NEVER raises.

    Worth its own row because it answers the question the other two cannot:
    memory that exists and memory that reaches a GENERATE prompt are different
    things, and this codebase has repeatedly shipped the first while believing
    it shipped the second.
    """
    try:
        flag = os.environ.get("JARVIS_MEMORY_ROUTING_ENABLED", "1")
        on = flag.strip().lower() not in ("0", "false", "no", "off")
        try:
            from backend.core.ouroboros.governance import module_routing  # noqa: F401
            present = "available"
        except Exception:  # noqa: BLE001
            present = "UNAVAILABLE"
        return (f"    routing: {'on' if on else 'off'} · "
                f"ModuleContextRouter {present}")
    except Exception:  # noqa: BLE001
        return "    routing: unknown"


# ---------------------------------------------------------------------------
# composition
# ---------------------------------------------------------------------------


def _clamp_payload(rows: List[str],
                   limit: int = _MAX_PAYLOAD_BYTES) -> List[str]:
    """Rows truncated to fit ``limit`` bytes, with a visible marker.

    The guard, not the design. This verb sends per-domain COUNTS and a capped
    hit list — never the corpus — so it is ~700 bytes today and nothing about
    it wants to grow. What this prevents is a LATER section, or a corpus an
    order of magnitude larger, quietly turning a summary into a flood that
    costs the TUI its frame budget across the socket.

    Truncates rather than refusing. An operator who asked what the organism
    knows should get a short answer that admits it was shortened, never an
    error — and never a silent tail-drop, which would read as "that is all
    there is". Pure. NEVER raises.
    """
    try:
        out: List[str] = []
        used = 0
        for row in rows or ():
            size = len(str(row).encode("utf-8", "replace")) + 1
            if used + size > int(limit):
                out.append(f"  [dim]… truncated at {int(limit)} bytes "
                           f"({len(rows) - len(out)} rows withheld)[/dim]")
                break
            out.append(row)
            used += size
        return out
    except Exception:  # noqa: BLE001
        return list(rows or ())


def compose_memory_lines(arg: str = "") -> List[str]:
    """Markup lines for ``/memory [search-term]``. NEVER raises.

    Pure composition — takes no console and prints nothing, so the daemon
    terminal and the attach cockpit render the SAME text through their own
    sinks. Returning lines rather than printing is what let this verb be
    mirrored at all; the ~76 handlers that print directly are invisible on
    the attach client for exactly that reason.
    """
    if not memory_surface_enabled():
        return ["  [dim]memory surface disabled "
                "(JARVIS_MEMORY_SURFACE_ENABLED=0)[/dim]"]
    try:
        term = str(arg or "").strip()
        out: List[str] = []

        if term:
            hits = search_topics(term)
            out.append(f"  [bold]memory · topics matching[/bold] {term!r}")
            out.extend(hits or ["    [dim](no topic name matches)[/dim]"])
            return _clamp_payload(out)

        out.append("  [bold]memory[/bold]")

        prefs, err = preference_rows()
        out.append("  [dim]taught by you[/dim]")
        if err:
            out.append(f"    [dim]unavailable — {err}[/dim]")
        elif prefs:
            out.extend(prefs)
        else:
            out.append("    [dim](nothing recorded yet)[/dim]")

        counts = topic_counts()
        total = sum(counts.values())
        out.append(f"  [dim]written topics[/dim] · {total}")
        if counts:
            ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:6]
            out.append("    " + " · ".join(f"{d} {n}" for d, n in ranked))
        else:
            out.append("    [dim](docs/memory_topics not found)[/dim]")

        out.append(_routing_row())
        out.append("  [dim]/memory topics <term> searches topic names[/dim]")
        return _clamp_payload(out)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[MemorySurface] compose degraded", exc_info=True)
        return [f"  [dim]memory surface degraded: "
                f"{type(exc).__name__}: {exc}[/dim]"]
