"""Frontier context — where the human's recent work ENDED, for dream attention.

Frontier Mapping (2026-07-18): for O+V to pick up where the human left off, the
DreamEngine's speculative attention must be directed at the modules the human
was actively building — the semantic FRONTIER — not uniformly at the whole tree.

DRY: composes the EXISTING ``git_momentum.compute_recent_momentum_async``
primitive (the same one StrategicDirection's digest uses — conventional-commit
scope/type histograms + latest subjects, zero model inference, loop-safe via
the dedicated git-read executor). No re-parsing of git, no new event bus; the
rendered block is dream-prompt hydration only.

Cached by HEAD sha (the dream candidate already carries ``repo_sha``): momentum
only changes when commits land, so idle-loop dream cycles cost zero git calls.
Fail-soft: any failure → "" and the dream proceeds without frontier direction.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

FRONTIER_CONTEXT_SCHEMA_VERSION = "frontier_context.v1"

_FALSY = ("0", "false", "no", "off")

# repo_sha -> rendered block (single live key; a new HEAD invalidates).
_cache: Dict[str, str] = {}


def frontier_context_enabled() -> bool:
    """``JARVIS_DREAM_FRONTIER_CONTEXT_ENABLED`` (default ON). NEVER raises."""
    return os.environ.get(
        "JARVIS_DREAM_FRONTIER_CONTEXT_ENABLED", "true",
    ).strip().lower() not in _FALSY


def _max_chars() -> int:
    """``JARVIS_FRONTIER_CONTEXT_MAX_CHARS`` (default 1400, floor 200). NEVER
    raises."""
    try:
        return max(200, int(os.environ.get("JARVIS_FRONTIER_CONTEXT_MAX_CHARS", "1400")))
    except (TypeError, ValueError):
        return 1400


def _max_commits() -> int:
    """``JARVIS_FRONTIER_MAX_COMMITS`` (default 50, floor 5). NEVER raises."""
    try:
        return max(5, int(os.environ.get("JARVIS_FRONTIER_MAX_COMMITS", "50")))
    except (TypeError, ValueError):
        return 50


def render_frontier_block(snapshot: "Optional[object]") -> str:
    """Render a MomentumSnapshot into the bounded frontier block. Pure. NEVER
    raises."""
    try:
        if snapshot is None or getattr(snapshot, "is_empty", lambda: True)():
            return ""
        scopes = getattr(snapshot, "top_scopes", lambda n=5: [])(5)
        types_ = getattr(snapshot, "top_types", lambda n=4: [])(4)
        subjects = list(getattr(snapshot, "latest_subjects", ()) or ())[:3]
        parts = [
            "## RECENT HUMAN FRONTIER (last "
            f"{int(getattr(snapshot, 'commit_count', 0) or 0)} commits — pick up "
            "where this work ENDED; prefer continuing these modules over "
            "unrelated areas)",
        ]
        if scopes:
            parts.append("Active scopes: " + ", ".join(
                f"{s}×{c}" for s, c in scopes
            ))
        if types_:
            parts.append("Change types: " + ", ".join(
                f"{t}×{c}" for t, c in types_
            ))
        if subjects:
            parts.append("Latest work:\n" + "\n".join(f"- {s}" for s in subjects))
        block = "\n".join(parts)
        cap = _max_chars()
        if len(block) > cap:
            block = block[: cap - 16].rstrip() + "\n[...truncated]"
        return block
    except Exception:  # noqa: BLE001
        return ""


async def frontier_context_async(
    repo_root: "Optional[str]" = None, repo_sha: str = "",
) -> str:
    """The bounded frontier block for dream hydration, sha-cached. "" when
    disabled / no momentum / any fault. NEVER raises."""
    try:
        if not frontier_context_enabled():
            return ""
        key = str(repo_sha or "")[:16] or "@nosha"
        cached = _cache.get(key)
        if cached is not None:
            return cached
        from backend.core.ouroboros.governance.git_momentum import (  # noqa: PLC0415
            compute_recent_momentum_async,
        )
        root = Path(repo_root or os.environ.get("JARVIS_REPO_ROOT", "."))
        snap = await compute_recent_momentum_async(
            root, max_commits=_max_commits(),
        )
        block = render_frontier_block(snap)
        _cache.clear()
        _cache[key] = block
        return block
    except Exception:  # noqa: BLE001 — hydration must never break a dream
        logger.debug("[FrontierContext] degraded", exc_info=True)
        return ""


def _reset_cache_for_tests() -> None:
    """Test helper. NEVER raises."""
    try:
        _cache.clear()
    except Exception:  # noqa: BLE001
        pass
