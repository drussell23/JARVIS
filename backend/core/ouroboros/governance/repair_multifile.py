"""L2 Repair Engine completion — Phase 1: topologically-ordered multi-file coordinated repair.

The L2 self-repair loop was single-file: a candidate carrying ``files: [...]`` (the multi-file shape
GENERATE/orchestrator already produce, with atomic batch-rollback via ``_apply_multi_file_candidate``)
was silently dropped, so any fault needing coordinated cross-file changes was unfixable by L2. This
module closes that asymmetry.

Pure + testable: extract the candidate's files, then **topologically sort** them by dependency
direction (a file others depend on is applied before its dependents) using the Oracle's lazy graph.
The L2 sandbox is itself the atomic transaction boundary — if any file in the batch fails to apply or
the tests fail, the throwaway sandbox is discarded, so the batch is all-or-nothing by construction.

Gated ``JARVIS_L2_MULTIFILE_ENABLED`` (default OFF → L2 stays single-file, byte-identical).
"""
from __future__ import annotations

import os
from typing import Any, Callable, List, Optional, Tuple

__all__ = [
    "l2_multifile_enabled",
    "extract_candidate_files",
    "topo_sort_files",
]

# A changed file: (file_path, full_content).
CandidateFile = Tuple[str, str]


def l2_multifile_enabled() -> bool:
    """``JARVIS_L2_MULTIFILE_ENABLED`` (default OFF) — let L2 repair coordinated multi-file candidates."""
    return os.environ.get("JARVIS_L2_MULTIFILE_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def extract_candidate_files(candidate: Any) -> List[CandidateFile]:
    """Canonical extraction of the changed files from a candidate dict (mirrors the orchestrator's
    ``files: [...]`` shape; falls back to the legacy single ``file_path``/``full_content`` pair).

    Returns only entries with a non-empty path AND string content (a real, usable change)."""
    if not isinstance(candidate, dict):
        return []
    files = candidate.get("files")
    out: List[CandidateFile] = []
    if isinstance(files, list) and files:
        for entry in files:
            if not isinstance(entry, dict):
                continue
            path = entry.get("file_path", "")
            content = entry.get("full_content", "")
            if path and isinstance(content, str) and content:
                out.append((path, content))
        return out
    # legacy single-file shape
    path = candidate.get("file_path", "")
    content = candidate.get("full_content", "")
    if path and isinstance(content, str) and content:
        out.append((path, content))
    return out


def topo_sort_files(
    files: List[CandidateFile],
    depends_on: Optional[Callable[[str, str], bool]] = None,
) -> List[CandidateFile]:
    """Order the changed files so a file's **dependencies are applied before it** (dependency →
    dependent). ``depends_on(a, b)`` returns True iff file *a* depends on file *b* (a imports/calls b).

    Deterministic Kahn's algorithm with stable tie-break on the candidate's original order. A
    dependency cycle among the changed files (or no ``depends_on`` provider) degrades gracefully to the
    original order — never raises, never drops a file."""
    paths = [p for p, _ in files]
    by_path = dict(files)
    if depends_on is None or len(files) <= 1:
        return list(files)

    order = {p: i for i, p in enumerate(paths)}
    # edge b -> a means "b must come before a" (a depends on b).
    deps: dict[str, set] = {p: set() for p in paths}   # p -> set of paths p depends on (within batch)
    for a in paths:
        for b in paths:
            if a == b:
                continue
            try:
                if depends_on(a, b):
                    deps[a].add(b)
            except Exception:  # noqa: BLE001 — graph lookups are best-effort
                continue

    resolved: List[str] = []
    remaining = set(paths)
    # Kahn: repeatedly emit nodes whose deps are all already resolved (stable by original order).
    progress = True
    while remaining and progress:
        progress = False
        ready = sorted(
            (p for p in remaining if deps[p] <= set(resolved)),
            key=lambda p: order[p],
        )
        for p in ready:
            resolved.append(p)
            remaining.discard(p)
            progress = True
    # Cycle remnant (or unresolved): append in original order — never drop.
    if remaining:
        resolved.extend(sorted(remaining, key=lambda p: order[p]))
    return [(p, by_path[p]) for p in resolved]
