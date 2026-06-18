"""Oracle graph traversal seam — interface-segregated backends for graph queries.

Slice 1 of the SQLite-Backed Lazy Traversal arc (`docs/architecture/ORACLE_LAZY_TRAVERSAL_ADD.md`).

Today every blast-radius / call-chain / dependency query runs over the whole in-memory NetworkX
``DiGraph`` (~5 GB for the 29k-file brain). This module introduces the seam that will let those
traversals read SQLite on demand instead — so the full graph becomes *queryable* on a constrained
host without being *resident*. Slice 1 ships the seam + the canonical in-memory backend + the live
differential parity harness; Slice 2 plugs a ``SqliteLazyGraphBackend`` into the harness's shadow
slot.

Design (mirrors ``PersistenceProvider``):
  - ``GraphBackend``           — ABC exposing the ~7 traversal primitives + default algorithms
    (``shortest_path`` / ``simple_cycles`` / ``descendants``) built ON those primitives, so any
    backend works without bespoke algorithm code.
  - ``InMemoryGraphBackend``   — canonical baseline over the live ``CodebaseKnowledgeGraph`` (the
    DEFAULT; byte-identical to the pre-seam path). Overrides the algorithms with NetworkX for speed.
  - ``DualBackendParityHarness`` — a backend façade wrapping a trusted ``primary`` + a candidate
    ``shadow``. Every lookup runs on both; on ANY divergence/shadow-error it captures a rich
    telemetry payload and **autonomously falls back to the primary result** — never crashes the
    control plane. This is how the SQLite backend earns trust in production before it's trusted alone.
  - ``AdaptiveNodeCache``      — bounded LRU (baseline 5,000 nodes) whose ``maxsize`` contracts under
    ``MemoryPressureGate`` pressure. Wired into the SQLite backend in Slice 3; defined+tested here.

The query API is synchronous (see ADD §2), so this whole layer is sync.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

EdgeRow = Tuple[str, Dict[str, Any]]  # (neighbor_key, edge_attrs)


# ---------------------------------------------------------------------------- config (§10)
def lazy_traversal_enabled() -> bool:
    """``JARVIS_ORACLE_LAZY_TRAVERSAL_ENABLED`` (default false) — route graph queries through the
    SQLite-backed lazy backend instead of the resident in-memory DiGraph. OFF → InMemory (today)."""
    return os.environ.get("JARVIS_ORACLE_LAZY_TRAVERSAL_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def parity_harness_enabled() -> bool:
    """``JARVIS_ORACLE_PARITY_HARNESS_ENABLED`` (default false) — run the candidate backend in the
    shadow slot of the DualBackendParityHarness, differentially verified against the trusted primary
    with autonomous fallback. The safe way to graduate the lazy backend in production."""
    return os.environ.get("JARVIS_ORACLE_PARITY_HARNESS_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def traversal_cache_max() -> int:
    """``JARVIS_ORACLE_TRAVERSAL_CACHE_MAX`` — baseline working-set node-cache size (§10 resolution:
    5,000 nodes). The adaptive contraction loop shrinks below this under host memory pressure."""
    try:
        return max(0, int(os.environ.get("JARVIS_ORACLE_TRAVERSAL_CACHE_MAX", "5000")))
    except (TypeError, ValueError):
        return 5000


# ---------------------------------------------------------------------------- the seam
class GraphBackend(ABC):
    """The traversal seam. Concrete backends implement the primitives; the algorithms below are
    built purely on those primitives so they work over any backend (in-memory, SQLite, future)."""

    # --- primitives (every backend MUST implement) ---
    @abstractmethod
    def contains(self, key: str) -> bool: ...

    @abstractmethod
    def get_node(self, key: str) -> Optional[Dict[str, Any]]:
        """Node attribute dict (the exact shape ``NodeData.to_dict()`` produces), or None."""

    @abstractmethod
    def successors(self, key: str) -> List[EdgeRow]:
        """Outgoing edges: ``[(dst_key, edge_attrs), ...]``."""

    @abstractmethod
    def predecessors(self, key: str) -> List[EdgeRow]:
        """Incoming edges: ``[(src_key, edge_attrs), ...]``."""

    @abstractmethod
    def node_count(self) -> int: ...

    @abstractmethod
    def edge_count(self) -> int: ...

    # --- algorithms over the primitives (overridable for native speed) ---
    def successor_keys(self, key: str) -> List[str]:
        return [dst for dst, _ in self.successors(key)]

    def predecessor_keys(self, key: str) -> List[str]:
        return [src for src, _ in self.predecessors(key)]

    def shortest_path(self, source: str, target: str) -> Optional[List[str]]:
        """Unweighted shortest path via BFS over ``successors`` (matches ``nx.shortest_path`` for an
        unweighted DiGraph). Returns the node-key path inclusive of endpoints, or None."""
        if source == target:
            return [source] if self.contains(source) else None
        if not self.contains(source) or not self.contains(target):
            return None
        prev: Dict[str, Optional[str]] = {source: None}
        q: deque = deque([source])
        while q:
            cur = q.popleft()
            for nxt in self.successor_keys(cur):
                if nxt in prev:
                    continue
                prev[nxt] = cur
                if nxt == target:
                    path = [nxt]
                    while prev[path[-1]] is not None:
                        path.append(prev[path[-1]])  # type: ignore[arg-type]
                    path.reverse()
                    return path
                q.append(nxt)
        return None

    def descendants(self, key: str, max_depth: Optional[int] = None) -> Set[str]:
        """All nodes reachable from ``key`` (exclusive of ``key``), optional depth bound."""
        seen: Set[str] = set()
        frontier: List[str] = [key]
        depth = 0
        while frontier and (max_depth is None or depth < max_depth):
            nxt: List[str] = []
            for n in frontier:
                for s in self.successor_keys(n):
                    if s not in seen and s != key:
                        seen.add(s)
                        nxt.append(s)
            frontier = nxt
            depth += 1
        return seen

    def simple_cycles(self, max_cycles: Optional[int] = None) -> List[List[str]]:
        """Enumerate elementary cycles via primitive-only DFS (Johnson-style backtracking). A global
        analysis — heavier than local traversals (ADD §6). Bounded by ``max_cycles`` when set."""
        cycles: List[List[str]] = []
        keys = list(self.all_keys())
        index = {k: i for i, k in enumerate(keys)}

        def dfs(start: str, node: str, stack: List[str], on_stack: Set[str]) -> None:
            if max_cycles is not None and len(cycles) >= max_cycles:
                return
            stack.append(node)
            on_stack.add(node)
            for nbr in self.successor_keys(node):
                if index.get(nbr, -1) < index.get(start, 1 << 62):
                    continue  # only consider nodes >= start (avoid duplicate rotations)
                if nbr == start:
                    cycles.append(list(stack))
                    if max_cycles is not None and len(cycles) >= max_cycles:
                        break
                elif nbr not in on_stack:
                    dfs(start, nbr, stack, on_stack)
            stack.pop()
            on_stack.discard(node)

        for k in keys:
            if max_cycles is not None and len(cycles) >= max_cycles:
                break
            dfs(k, k, [], set())
        return cycles

    def all_keys(self) -> List[str]:
        """All node keys. Default raises — backends that support global scans override it."""
        raise NotImplementedError("backend does not support full key enumeration")


# ---------------------------------------------------------------------------- in-memory baseline
class InMemoryGraphBackend(GraphBackend):
    """Canonical backend over a live ``CodebaseKnowledgeGraph`` (read dynamically, so wholesale
    ``_graph`` replacement on cache load is transparent). Overrides the algorithms with NetworkX."""

    def __init__(self, ckg: Any):
        self._ckg = ckg

    @property
    def _g(self):
        return self._ckg._graph

    def contains(self, key: str) -> bool:
        return key in self._g

    def get_node(self, key: str) -> Optional[Dict[str, Any]]:
        g = self._g
        return dict(g.nodes[key]) if key in g else None

    def successors(self, key: str) -> List[EdgeRow]:
        g = self._g
        if key not in g:
            return []
        return [(dst, dict(g.edges[key, dst])) for dst in g.successors(key)]

    def predecessors(self, key: str) -> List[EdgeRow]:
        g = self._g
        if key not in g:
            return []
        return [(src, dict(g.edges[src, key])) for src in g.predecessors(key)]

    def node_count(self) -> int:
        return self._g.number_of_nodes()

    def edge_count(self) -> int:
        return self._g.number_of_edges()

    def all_keys(self) -> List[str]:
        return list(self._g.nodes)

    # native NetworkX overrides (exact + fast)
    def shortest_path(self, source: str, target: str) -> Optional[List[str]]:
        import networkx as nx
        g = self._g
        if source not in g or target not in g:
            return None
        try:
            return nx.shortest_path(g, source, target)
        except nx.NetworkXNoPath:
            return None

    def simple_cycles(self, max_cycles: Optional[int] = None) -> List[List[str]]:
        import networkx as nx
        out: List[List[str]] = []
        for cyc in nx.simple_cycles(self._g):
            out.append(cyc)
            if max_cycles is not None and len(out) >= max_cycles:
                break
        return out


# ---------------------------------------------------------------------------- live differential harness
class DualBackendParityHarness(GraphBackend):
    """Weaponized live differential guard. Wraps a trusted ``primary`` + candidate ``shadow``; runs
    every read on both, and on ANY divergence or shadow error captures a telemetry payload and
    returns the PRIMARY result — never crashing the control plane. The primary's result is always
    authoritative, so the shadow can be wrong/absent without any availability impact."""

    def __init__(
        self,
        primary: GraphBackend,
        shadow: Optional[GraphBackend] = None,
        *,
        on_divergence: Optional[Callable[[Dict[str, Any]], None]] = None,
        max_records: int = 100,
    ):
        self.primary = primary
        self.shadow = shadow
        self._on_divergence = on_divergence
        self.comparisons = 0
        self.divergences = 0
        self.shadow_errors = 0
        self.records: deque = deque(maxlen=max(1, max_records))

    # -- the differential wrapper --
    def _diff(self, method: str, args: tuple, primary_val: Any) -> Any:
        """Run the shadow, compare to ``primary_val``, record + fall back on any mismatch/error.
        Always returns ``primary_val`` (the trusted path)."""
        if self.shadow is None:
            return primary_val
        self.comparisons += 1
        try:
            shadow_val = getattr(self.shadow, method)(*args)
        except Exception as exc:  # noqa: BLE001 — shadow must never break the query
            self.shadow_errors += 1
            self._record(method, args, primary_val, f"<shadow raised: {exc!r}>", kind="shadow_error")
            return primary_val
        if not _results_equal(primary_val, shadow_val):
            self.divergences += 1
            self._record(method, args, primary_val, shadow_val, kind="divergence")
        return primary_val

    def _record(self, method: str, args: tuple, primary_val: Any, shadow_val: Any, *, kind: str) -> None:
        payload = {
            "kind": kind,
            "method": method,
            "args": [str(a) for a in args],
            "primary": _summarize(primary_val),
            "shadow": _summarize(shadow_val),
        }
        self.records.append(payload)
        logger.warning(
            "[OracleParity] %s on %s%s — primary=%s shadow=%s (autonomous fallback to primary)",
            kind, method, payload["args"], payload["primary"], payload["shadow"],
        )
        if self._on_divergence is not None:
            try:
                self._on_divergence(payload)
            except Exception:  # noqa: BLE001 — telemetry sink must never break the query
                logger.debug("[OracleParity] on_divergence sink failed", exc_info=True)

    # -- primitives (primary authoritative, shadow differentially checked) --
    def contains(self, key: str) -> bool:
        return self._diff("contains", (key,), self.primary.contains(key))

    def get_node(self, key: str) -> Optional[Dict[str, Any]]:
        return self._diff("get_node", (key,), self.primary.get_node(key))

    def successors(self, key: str) -> List[EdgeRow]:
        return self._diff("successors", (key,), self.primary.successors(key))

    def predecessors(self, key: str) -> List[EdgeRow]:
        return self._diff("predecessors", (key,), self.primary.predecessors(key))

    def node_count(self) -> int:
        return self._diff("node_count", (), self.primary.node_count())

    def edge_count(self) -> int:
        return self._diff("edge_count", (), self.primary.edge_count())

    def all_keys(self) -> List[str]:
        return self.primary.all_keys()

    # algorithms: trust the primary's (overridden) impl, differentially check the shadow's
    def shortest_path(self, source: str, target: str) -> Optional[List[str]]:
        return self._diff("shortest_path", (source, target), self.primary.shortest_path(source, target))

    def simple_cycles(self, max_cycles: Optional[int] = None) -> List[List[str]]:
        return self._diff("simple_cycles", (max_cycles,), self.primary.simple_cycles(max_cycles))

    def stats(self) -> Dict[str, int]:
        return {
            "comparisons": self.comparisons,
            "divergences": self.divergences,
            "shadow_errors": self.shadow_errors,
            "records": len(self.records),
        }


def _results_equal(a: Any, b: Any) -> bool:
    """Order-insensitive structural equality for primitive results (edge lists are sets of
    (neighbor, sorted-attr-items); node dicts compared directly)."""
    if isinstance(a, list) and isinstance(b, list):
        try:
            return _norm_edges(a) == _norm_edges(b)
        except (TypeError, ValueError):
            return a == b
    return a == b


def _norm_edges(rows: list):
    norm = set()
    for r in rows:
        if isinstance(r, tuple) and len(r) == 2 and isinstance(r[1], dict):
            norm.add((r[0], tuple(sorted((k, _h(v)) for k, v in r[1].items()))))
        else:
            norm.add(_h(r))
    return norm


def _h(v: Any):
    return tuple(sorted(v.items())) if isinstance(v, dict) else (tuple(v) if isinstance(v, list) else v)


def _summarize(v: Any) -> str:
    if isinstance(v, list):
        return f"list[{len(v)}]"
    if isinstance(v, dict):
        return f"dict{{{','.join(sorted(v)[:4])}}}"
    s = repr(v)
    return s if len(s) <= 80 else s[:77] + "..."


# ---------------------------------------------------------------------------- adaptive working-set cache
class AdaptiveNodeCache:
    """Bounded LRU for traversal working-set nodes (baseline 5,000). ``maxsize`` contracts under
    host memory pressure — the query-side mirror of the index-side Memory Armor. Sync hot path;
    contraction is driven by :meth:`apply_pressure` (a pressure-level string). Wired into the SQLite
    backend in Slice 3; standalone + tested here."""

    _PRESSURE_FRAC = {"ok": 1.0, "warn": 1.0, "high": 0.5, "critical": 0.1}

    def __init__(self, baseline: Optional[int] = None):
        self._baseline = traversal_cache_max() if baseline is None else max(0, baseline)
        self._maxsize = self._baseline
        self._d: "OrderedDict[str, Any]" = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    @property
    def maxsize(self) -> int:
        return self._maxsize

    def get(self, key: str) -> Any:
        if key in self._d:
            self._d.move_to_end(key)
            self.hits += 1
            return self._d[key]
        self.misses += 1
        return None

    def put(self, key: str, value: Any) -> None:
        if self._maxsize <= 0:
            return
        self._d[key] = value
        self._d.move_to_end(key)
        self._trim()

    def apply_pressure(self, level: str) -> int:
        """Recompute ``maxsize`` from the pressure level and evict down to it. Returns evicted count.
        CRITICAL also signals the caller to GC (handled by the caller, which holds gc)."""
        frac = self._PRESSURE_FRAC.get(level, 1.0)
        self._maxsize = int(self._baseline * frac)
        return self._trim()

    def _trim(self) -> int:
        n = 0
        while len(self._d) > self._maxsize:
            self._d.popitem(last=False)  # evict LRU
            self.evictions += 1
            n += 1
        return n

    def __len__(self) -> int:
        return len(self._d)


# ---------------------------------------------------------------------------- factory
def build_graph_backend(ckg: Any) -> GraphBackend:
    """Single decision point for the traversal backend. Slice 1: always returns
    ``InMemoryGraphBackend`` (byte-identical to the pre-seam path). Slice 2 will return a
    ``DualBackendParityHarness(primary=InMemory, shadow=SqliteLazy)`` when the lazy + parity flags
    are on — so the Oracle's query layer never needs to change again."""
    return InMemoryGraphBackend(ckg)
