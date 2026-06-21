# backend/core/ouroboros/governance/epistemic_prefetch.py
"""DAG Router — directed pre-fetch for heavy GOALs (spec section 5.1).

On a heavy multi-file GOAL, ask the (already-booted) Oracle for a fused
structural+semantic neighborhood, rank + bound it, snapshot each candidate's
sha256 (Truth Guard), and return an immutable manifest that seeds Venom so it
starts DIRECTED instead of blind. Gated, fail-soft, no-op unless heavy + oracle
ready. Never blocks GENERATE on the oracle.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from backend.core.ouroboros.governance.epistemic_quarantine import atomic_read_and_hash

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_EPISTEMIC_PREFETCH_ENABLED"
_ENV_TOPK = "JARVIS_EPISTEMIC_PREFETCH_TOPK"
_ENV_SEED_BYTES = "JARVIS_EPISTEMIC_PREFETCH_SEED_BYTES"


@dataclass(frozen=True)
class PrefetchEntry:
    rel_path: str
    sha256: str
    relevance: float
    category_hint: str
    content_excerpt: str


def prefetch_enabled() -> bool:
    return (os.environ.get(_ENV_ENABLED, "true") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _topk() -> int:
    try:
        return max(1, int((os.environ.get(_ENV_TOPK, "") or "8").strip()))
    except (TypeError, ValueError):
        return 8


def _seed_bytes() -> int:
    try:
        return max(0, int((os.environ.get(_ENV_SEED_BYTES, "") or "24000").strip()))
    except (TypeError, ValueError):
        return 24000


def _field(item: Any, key: str, default):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


async def build_prefetch_manifest(
    *,
    target_files: Tuple[str, ...],
    root: str,
    oracle: Optional[Any],
    goal_text: str,
    is_heavy: bool,
) -> Tuple[PrefetchEntry, ...]:
    """Return a bounded, ranked, hash-validated candidate manifest. () when
    disabled / not heavy / oracle cold / on any error (fail-soft)."""
    try:
        if not prefetch_enabled() or not is_heavy or oracle is None:
            return ()
        if not bool(oracle.is_semantic_ready()):
            return ()
        topk = _topk()
        neighborhood = await oracle.get_fused_neighborhood(
            list(target_files), goal_text, k_semantic=topk)
        if not neighborhood:
            return ()
        ranked = sorted(
            neighborhood,
            key=lambda it: float(_field(it, "score", 0.0) or 0.0),
            reverse=True,
        )[:topk]

        budget = _seed_bytes()
        spent = 0
        entries = []
        targets = set(target_files)
        for it in ranked:
            rel = str(_field(it, "rel_path", "") or "")
            if not rel or rel in targets:
                continue
            data, digest = atomic_read_and_hash(os.path.join(root, rel))
            if not digest:
                continue
            excerpt = ""
            text = data.decode("utf-8", errors="replace")
            tlen = len(text.encode("utf-8"))
            if spent + tlen <= budget:
                excerpt = text
                spent += tlen
            entries.append(PrefetchEntry(
                rel_path=rel,
                sha256=digest,
                relevance=float(_field(it, "score", 0.0) or 0.0),
                category_hint=str(_field(it, "category_hint", "COMPREHENSION")
                                  or "COMPREHENSION"),
                content_excerpt=excerpt,
            ))
        logger.info(
            "[EpistemicPrefetch] candidates=%d seeded=%d bytes=%d",
            len(entries), sum(1 for e in entries if e.content_excerpt), spent)
        return tuple(entries)
    except Exception:  # noqa: BLE001 — never block GENERATE on prefetch
        logger.debug("[EpistemicPrefetch] build swallowed", exc_info=True)
        return ()
