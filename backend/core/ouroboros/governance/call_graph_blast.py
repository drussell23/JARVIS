"""Call-graph blast radius — symbol-scoped risk over the EXISTING oracle
call graph (Sovereign Call-Graph Risk Matrix, C1 root-cause).

Diagnosis (review finding C1)
-----------------------------
``operation_advisor._compute_blast_radius`` counts files that import a
*module* (the IMPORT graph), capped at 50. A sub-goal scoped to a single
8-line method on ``semantic_index.py`` (imported by 50 files) therefore
inherits the whole file's blast radius and can never clear the BLOCK
veto. The AST symbol scope produced by ``ast_symbol_scoper.isolate_symbols``
is real but invisible to the Advisor.

Fix
---
When a sub-goal carries an AST symbol scope (``file::Symbol``), measure
blast radius over the transitive *callers* of those symbols (the CALL
graph — who actually executes the changed code), NOT file size. The call
graph is ALREADY built by the oracle (``oracle.get_callers`` +
``oracle.find_nodes_by_name``); this module only QUERIES it — it builds no
parallel graph.

Invariants
----------
* Pure + fail-soft: NEVER raises. Any oracle / resolution error → ``None``
  (the caller falls back to the legacy file-level computation EXACTLY).
* Same-or-sharper: a widely-called symbol still measures high and stays
  BLOCKed (the gate is never weakened — only the *input* gets sharper).
* Bounded: the caller BFS is depth- + fan-bounded (env curve) so a
  pathological hub symbol cannot blow the Advisor's <60s budget.
* Capped at 50 for comparability with the legacy import-graph ceiling.
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# Cap the reported blast count to match the legacy import-scan ceiling
# (operation_advisor._compute_blast_radius breaks at 50). Keeps the
# downstream risk-score calibration identical between the two paths.
_BLAST_CAP: int = 50


def _max_depth() -> int:
    """``JARVIS_CALLGRAPH_BLAST_MAX_DEPTH`` (default 4) — transitive caller
    BFS depth bound. Keeps the closure shallow enough to respect the
    Advisor's <60s budget on a pathological hub symbol."""
    try:
        return max(1, int(os.environ.get("JARVIS_CALLGRAPH_BLAST_MAX_DEPTH", "4")))
    except (ValueError, TypeError):
        return 4


def _max_fan() -> int:
    """``JARVIS_CALLGRAPH_BLAST_MAX_FAN`` (default 64) — per-node caller
    fan-out bound. Bounds the breadth of each BFS expansion."""
    try:
        return max(1, int(os.environ.get("JARVIS_CALLGRAPH_BLAST_MAX_FAN", "64")))
    except (ValueError, TypeError):
        return 64


def _split_scope(scoped: str) -> Tuple[str, str]:
    """Split a ``"file::Symbol"`` ref into ``(file_path, symbol)``.

    ``rsplit('::', 1)`` so file paths that themselves contain ``::``
    (unlikely, but safe) keep the LAST segment as the symbol. Returns
    ``("", "")`` for a malformed ref (no ``::``), which never resolves.
    """
    if "::" not in scoped:
        return "", ""
    file_part, _, symbol = scoped.rpartition("::")
    return file_part, symbol


def _leaf_name(symbol: str) -> str:
    """The oracle indexes nodes by their entity ``name`` (e.g. ``render``
    or ``Widget.render``). ``find_nodes_by_name`` matches the exact name
    OR a ``.<name>`` suffix, so passing the full ``Class.method`` works;
    we pass the symbol through unchanged (it already matches)."""
    return symbol


def _resolve_nodes(
    oracle: Any, file_path: str, symbol: str,
) -> Optional[List[Any]]:
    """Resolve a ``file::Symbol`` to oracle NodeID(s).

    Returns the matching node list, or ``None`` if the oracle errors.
    An empty list means "resolved cleanly but no such symbol" — the
    caller treats that as unresolved (→ file-level fallback).

    Disambiguation: when several nodes share the leaf name, prefer the
    ones whose ``file_path`` basename matches the scoped file's basename.
    """
    try:
        candidates = oracle.find_nodes_by_name(_leaf_name(symbol))
    except Exception:  # noqa: BLE001 — fail-soft, never raise
        return None
    if not candidates:
        return []
    # Disambiguate by file basename when the scoped file is known.
    if file_path:
        want = os.path.basename(file_path)
        narrowed = []
        for node in candidates:
            node_fp = getattr(node, "file_path", "") or ""
            if node_fp and os.path.basename(node_fp) == want:
                narrowed.append(node)
        if narrowed:
            return narrowed
    return list(candidates)


def _transitive_caller_count(
    oracle: Any,
    seeds: Sequence[Any],
    *,
    max_depth: int,
    max_fan: int,
    cap: int,
) -> Optional[int]:
    """BFS the transitive ``get_callers`` closure of ``seeds``.

    Counts DISTINCT caller nodes (dedup by ``str(node)``), bounded by
    ``max_depth`` (BFS levels) and ``max_fan`` (callers expanded per
    node), capped at ``cap``. Returns ``None`` on any oracle error
    (fail-soft); the seeds themselves are NOT counted (they are the
    changed symbols, not their blast).
    """
    seen: Set[str] = set()
    # Frontier of (node, depth). Seeds are depth 0; their callers depth 1.
    frontier: List[Tuple[Any, int]] = [(s, 0) for s in seeds]
    # Seeds are the changed symbols, NOT their blast — exclude them from
    # the count so a symbol that calls itself isn't counted as its caller.
    seed_keys: Set[str] = set()
    for s in seeds:
        try:
            seed_keys.add(str(s))
        except Exception:  # noqa: BLE001
            return None

    while frontier:
        node, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        try:
            callers = oracle.get_callers(node)
        except Exception:  # noqa: BLE001 — fail-soft
            return None
        if not callers:
            continue
        for caller in callers[:max_fan]:
            try:
                key = str(caller)
            except Exception:  # noqa: BLE001
                return None
            if key in seed_keys or key in seen:
                continue
            seen.add(key)
            if len(seen) >= cap:
                return cap
            frontier.append((caller, depth + 1))
    return len(seen)


def symbol_blast_radius(
    scoped_symbols: Sequence[str],
    *,
    oracle: Any,
) -> Optional[int]:
    """Blast radius of an AST symbol-scoped op over the oracle call graph.

    For each ``"file::Symbol"`` ref: resolve the oracle ``NodeID`` and BFS
    the transitive ``get_callers`` closure (who calls those symbols),
    bounded by depth + fan (env curve) and dedup'd by node identity.
    Returns the count of DISTINCT caller symbols across all scoped symbols,
    capped at 50 for comparability with the legacy import-scan ceiling.

    Returns ``None`` when:
      * ``oracle`` is ``None`` or ``scoped_symbols`` is empty,
      * any scoped symbol cannot be resolved to a graph node, OR
      * any oracle query raises.

    A ``None`` return is the signal to fall back to the file-level
    import-graph computation EXACTLY (OFF byte-identical / fail-soft).
    NEVER raises.
    """
    if oracle is None or not scoped_symbols:
        return None

    max_depth = _max_depth()
    max_fan = _max_fan()

    # Resolve every scoped symbol to seed nodes FIRST. Conservative
    # semantics (mirrors _oracle_blast_count): if ANY symbol is
    # unresolvable, abort the call-graph path so the legacy file-level
    # scan still catches name occurrences across the tree.
    seeds: List[Any] = []
    seed_keys: Set[str] = set()
    for scoped in scoped_symbols:
        file_path, symbol = _split_scope(scoped)
        if not symbol:
            # Whole-file marker (scoper fallback) or malformed ref — the
            # call graph has nothing to scope to. Abort → file-level.
            return None
        nodes = _resolve_nodes(oracle, file_path, symbol)
        if nodes is None:
            return None  # oracle error → fail-soft
        if not nodes:
            return None  # symbol not in graph → file-level fallback
        for node in nodes:
            try:
                key = str(node)
            except Exception:  # noqa: BLE001
                return None
            if key not in seed_keys:
                seed_keys.add(key)
                seeds.append(node)

    if not seeds:
        return None

    count = _transitive_caller_count(
        oracle,
        seeds,
        max_depth=max_depth,
        max_fan=max_fan,
        cap=_BLAST_CAP,
    )
    if count is None:
        return None
    return min(count, _BLAST_CAP)


__all__ = ["symbol_blast_radius"]
