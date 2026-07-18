"""North-Star context — deterministic architectural-intent hydration for dreams.

Semantic Context Engineering (2026-07-18): the DreamEngine optimizes technical
entropy (doc staleness, code rot) but is blind to the MACRO intent — that this
repo is an interconnected alliance system (Apple/GCP/DoubleWord/Claude) with an
explicit North Star. Its SPECULATIVE hypotheses should align with FEATURE
PROGRESSION toward the PRD's A-level criteria, not merely hunt rot.

This module is the foundational-context supplier: a DETERMINISTIC, bounded,
mtime-cached extraction of the two intent documents —

  * ``docs/architecture/OUROBOROS_VENOM_PRD.md``  (the phased roadmap; §6 holds
    the A-level "definition of DONE" table)
  * ``docs/architecture/OUROBOROS_VENOM_NORTH_STAR.md``  (§51 North Star Galaxy)

Composition (in priority order, truncated to the char budget):
  1. The PRD §6 A-level criteria section VERBATIM — the definition of DONE the
     organism should be closing the gap toward.
  2. The section-header OUTLINE of both docs (##/### lines) — the map of intent
     without the 750KB of prose.

Explicitly NOT a RAG pipeline (mandate 3): plain file reads + header scans, no
embeddings, no retrieval index, no model calls. Cached by (path, mtime, budget)
so idle-loop dream cycles never re-read 750KB. Fail-soft everywhere: a missing
doc contributes nothing and the dream proceeds unhydrated (never raises).
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

NORTH_STAR_CONTEXT_SCHEMA_VERSION = "north_star_context.v1"

_FALSY = ("0", "false", "no", "off")

# (path, mtime, budget) -> rendered block
_cache: Dict[Tuple[str, float, float, int], str] = {}


def north_star_hydration_enabled() -> bool:
    """``JARVIS_DREAM_NORTH_STAR_CONTEXT_ENABLED`` (default ON). Off → dreams
    are byte-identical to the pre-hydration prompt. NEVER raises."""
    return os.environ.get(
        "JARVIS_DREAM_NORTH_STAR_CONTEXT_ENABLED", "true",
    ).strip().lower() not in _FALSY


def _max_chars() -> int:
    """``JARVIS_NORTH_STAR_CONTEXT_MAX_CHARS`` (default 6000, floor 500) — the
    hydration block's hard budget so it can never crowd the dream's own context
    window. NEVER raises."""
    try:
        n = int(os.environ.get("JARVIS_NORTH_STAR_CONTEXT_MAX_CHARS", "6000"))
        return max(500, n)
    except (TypeError, ValueError):
        return 6000


def _prd_path(repo_root: Path) -> Path:
    return Path(os.environ.get(
        "JARVIS_PRD_DOC", str(repo_root / "docs/architecture/OUROBOROS_VENOM_PRD.md"),
    ))


def _north_star_path(repo_root: Path) -> Path:
    return Path(os.environ.get(
        "JARVIS_NORTH_STAR_DOC",
        str(repo_root / "docs/architecture/OUROBOROS_VENOM_NORTH_STAR.md"),
    ))


def _section(text: str, header_pat: str, max_lines: int = 40) -> str:
    """Lines from the first header matching *header_pat* until the next header of
    the same-or-higher level (or *max_lines*). Empty when absent. NEVER raises."""
    try:
        lines = text.splitlines()
        start = level = None
        for i, ln in enumerate(lines):
            if re.match(header_pat, ln):
                start = i
                level = len(ln) - len(ln.lstrip("#"))
                break
        if start is None:
            return ""
        out = [lines[start]]
        for ln in lines[start + 1:start + max_lines]:
            m = re.match(r"^(#{1,6}) ", ln)
            if m and len(m.group(1)) <= (level or 6):
                break
            out.append(ln)
        return "\n".join(out).strip()
    except Exception:  # noqa: BLE001
        return ""


def _outline(text: str, max_headers: int = 60) -> str:
    """The ##/### header lines — the intent map without the prose. NEVER raises."""
    try:
        heads = [
            ln.strip() for ln in text.splitlines()
            if re.match(r"^#{2,3} ", ln)
        ]
        return "\n".join(heads[:max_headers])
    except Exception:  # noqa: BLE001
        return ""


def north_star_context(repo_root: "Optional[str]" = None) -> str:
    """The bounded architectural-intent block for dream-prompt hydration.

    Returns "" when disabled or neither doc is readable (fail-soft — the dream
    proceeds unhydrated). Cached by (paths, mtimes, budget). NEVER raises."""
    try:
        if not north_star_hydration_enabled():
            return ""
        root = Path(repo_root or os.environ.get("JARVIS_REPO_ROOT", "."))
        prd, ns = _prd_path(root), _north_star_path(root)
        budget = _max_chars()

        def _mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return -1.0

        key = (str(prd), _mtime(prd), _mtime(ns), budget)
        cached = _cache.get(key)
        if cached is not None:
            return cached

        parts = []
        prd_text = ""
        try:
            prd_text = prd.read_text(errors="replace")
        except OSError:
            pass
        ns_text = ""
        try:
            ns_text = ns.read_text(errors="replace")
        except OSError:
            pass
        if not prd_text and not ns_text:
            _cache.clear()
            _cache[key] = ""
            return ""

        if prd_text:
            a_level = _section(prd_text, r"^## 6\. Target State", max_lines=30)
            if a_level:
                parts.append(
                    "### The A-level definition of DONE (PRD §6 — close THIS gap):\n"
                    + a_level
                )
            parts.append("### PRD roadmap outline:\n" + _outline(prd_text))
        if ns_text:
            parts.append("### North Star Galaxy outline (§51):\n" + _outline(ns_text, 40))

        block = (
            "## ARCHITECTURAL INTENT (North Star / PRD — deterministic extract)\n"
            "JARVIS is an interconnected alliance system bridging Apple (macOS "
            "body), GCP (J-Prime mind), DoubleWord and Claude (cognition tiers). "
            "Speculative hypotheses should ADVANCE this roadmap — prefer feature "
            "progression toward the A-level criteria below over cosmetic rot.\n\n"
            + "\n\n".join(p for p in parts if p)
        )
        if len(block) > budget:
            block = block[: budget - 24].rstrip() + "\n[...intent truncated]"
        # Bound the cache itself (one live key; mtime/budget changes invalidate).
        _cache.clear()
        _cache[key] = block
        return block
    except Exception:  # noqa: BLE001 — hydration must never break a dream
        logger.debug("[NorthStarContext] extraction degraded", exc_info=True)
        return ""


def _reset_cache_for_tests() -> None:
    """Test helper. NEVER raises."""
    try:
        _cache.clear()
    except Exception:  # noqa: BLE001
        pass
